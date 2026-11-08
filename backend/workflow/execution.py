"""Start a Segment as a real bounded run and reconcile its terminal state."""

from __future__ import annotations

import threading
import time
from typing import Any

from .. import snapshots, storage
from ..supervisor import ActiveRunError
from ..models import DomainStateRef, FixedRandom, ProjectV7
from ..validation import ProjectInputError, validate_project
from .hashing import full_project_hash
from .metrology import snapshot_content_hash
from .routes import CheckpointRefV1, SegmentV1
from .store import WorkflowConflict, WorkflowNotFound, WorkflowStore


TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
CANCEL_POLL_INTERVAL_SECONDS = 0.2
CANCEL_TIMEOUT_SECONDS = 30.0


def _load_continuation(segment: SegmentV1) -> dict[str, Any] | None:
    if segment.startAfterStepId is None:
        return None
    checkpoint = segment.startCheckpointRef
    if checkpoint is None:
        raise WorkflowConflict("continued Segment requires a start checkpoint")
    run_dir = storage.run_path(checkpoint.runId)
    manifest = storage.read_json(run_dir / "manifest.json")
    row = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == checkpoint.stepId), None)
    state = None if row is None else row.get("nativeState")
    if not isinstance(state, dict):
        raise WorkflowConflict("start checkpoint has no native Domain state")
    if state.get("stateId") != checkpoint.stateHash:
        raise WorkflowConflict("start checkpoint state hash does not match the manifest")
    fields = set(DomainStateRef.model_fields)
    reference = {key: value for key, value in state.items() if key in fields}
    try:
        return DomainStateRef.model_validate(reference).model_dump(mode="json", exclude_none=True)
    except ValueError as exc:
        raise WorkflowConflict(f"start checkpoint native state is invalid: {exc}") from exc


def build_segment_project(store: WorkflowStore, segment_id: str) -> ProjectV7:
    segment = store.get_segment(segment_id)
    attempt = store.get_attempt(segment.attemptId)
    compile_record = store.get_compile(segment.sourceCompileId)
    full = compile_record.project
    step_ids = [step.id for step in full.steps]
    start = 0 if segment.startAfterStepId is None else (
        step_ids.index(segment.startAfterStepId) + 1 if segment.startAfterStepId in step_ids else -1
    )
    end = step_ids.index(segment.throughStepId) if segment.throughStepId in step_ids else -1
    if start < 0 or end < 0 or start > end:
        raise WorkflowConflict("Segment range does not select a valid compile slice")
    payload = full.model_dump(mode="json", exclude_none=True)
    payload["name"] = f"{full.name} · {segment.segmentId}"
    payload["steps"] = [step.model_dump(mode="json", exclude_none=True) for step in full.steps[start:end + 1]]
    solver = dict(payload["solver"])
    solver["random"] = FixedRandom(mode="fixed", seed=attempt.runSeed).model_dump(mode="json")
    payload["solver"] = solver
    continuation = _load_continuation(segment)
    if continuation is None:
        payload.pop("continuationState", None)
    else:
        payload["continuationState"] = continuation
    try:
        project = ProjectV7.model_validate(payload)
        validated, _warnings = validate_project(project.model_dump(mode="json", exclude_none=True), kind="simulate")
        return validated
    except (ValueError, ProjectInputError) as exc:
        raise WorkflowConflict(f"Segment project is invalid: {exc}") from exc


def _finish_from_manifest(store: WorkflowStore, segment_id: str, run_id: str, status: str) -> None:
    end_checkpoint: CheckpointRefV1 | None = None
    provenance_input_hash = None
    execution_hash = None
    result_digest = None
    try:
        run_dir = storage.run_path(run_id)
        manifest = storage.read_json(run_dir / "manifest.json")
        provenance_input_hash = manifest.get("requestedInputHash")
        execution_hash = manifest.get("executionHash")
        segment = store.get_segment(segment_id)
        row = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == segment.throughStepId), None)
        try:
            result_path = snapshots.ensure_materialized(run_dir, segment.throughStepId)
            result_digest = snapshot_content_hash(storage.read_json(result_path))
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            result_digest = None
        state = None if row is None else row.get("nativeState")
        if isinstance(state, dict) and state.get("stateId"):
            end_checkpoint = CheckpointRefV1(
                checkpointId=f"cp_{run_id}_{segment.throughStepId}",
                runId=run_id,
                stepId=segment.throughStepId,
                stateHash=state["stateId"],
            )
    except (FileNotFoundError, OSError, ValueError):
        end_checkpoint = None
    final_status = "failed" if status == "timed_out" else status
    if final_status == "succeeded" and end_checkpoint is None:
        final_status = "failed"
    store.finish_segment(
        segment_id,
        final_status,
        end_checkpoint,
        provenance_input_hash=provenance_input_hash,
        execution_hash=execution_hash,
        result_digest=result_digest,
    )


def _monitor(store: WorkflowStore, supervisor: Any, segment_id: str, run_id: str) -> None:
    while True:
        try:
            status = str(supervisor.get_run(run_id).get("status"))
        except (FileNotFoundError, OSError, ValueError):
            status = "interrupted"
        if status in TERMINAL:
            try:
                _finish_from_manifest(store, segment_id, run_id, status)
            except Exception:
                pass
            return
        time.sleep(0.2)


class SegmentBusy(WorkflowConflict):
    """The supervisor is already running another job; retry later."""


def _run_status(supervisor: Any, run_id: str) -> str:
    try:
        return str(supervisor.get_run(run_id).get("status"))
    except (FileNotFoundError, OSError, ValueError):
        # A run directory that disappeared can never reach a terminal state
        # again; the monitor thread records it as interrupted.
        return "interrupted"


def _stop_active_run(supervisor: Any, run_id: str) -> bool:
    """Cancel the run holding the single supervisor slot and wait for it.

    The old Segment is left to its own monitor thread: touching its status here
    would race with that thread's terminal transition. Returns False when the
    run is still active after the timeout so the caller can report a busy slot
    instead of starting a second worker.
    """

    try:
        supervisor.cancel(run_id)
    except Exception as exc:
        raise SegmentBusy("上一个运行未能停止，请稍后重试") from exc
    deadline = time.monotonic() + CANCEL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _run_status(supervisor, run_id) in TERMINAL:
            return True
        time.sleep(CANCEL_POLL_INTERVAL_SECONDS)
    return False


def _start_run(supervisor: Any, project: dict[str, Any]) -> dict[str, Any]:
    """Start the project, preempting the run that currently holds the slot.

    One supervisor slot is a process-wide resource; a user who starts another
    Segment means "replace the old run", not "fail with an English conflict".
    The old run is cancelled and awaited first, then exactly one retry starts
    the new run. A still-busy slot after that is a real conflict.
    """

    try:
        return supervisor.start("simulate", project)
    except ActiveRunError as exc:
        if not _stop_active_run(supervisor, exc.run_id):
            raise SegmentBusy(f"上一个运行停止超时：{exc.run_id}") from exc
        try:
            return supervisor.start("simulate", project)
        except ActiveRunError as retry_exc:
            raise SegmentBusy("上一个运行未能停止，请稍后重试") from retry_exc
        except Exception as exc:
            raise WorkflowConflict(f"segment run could not start: {exc}") from exc
    except Exception as exc:
        raise WorkflowConflict(f"segment run could not start: {exc}") from exc


def execute_segment(store: WorkflowStore, supervisor: Any, segment_id: str) -> tuple[SegmentV1, str]:
    segment = store.get_segment(segment_id)
    if segment.status != "planned":
        raise WorkflowConflict(f"segment {segment_id!r} is not planned")
    plan = store.get_plan(store.get_attempt(segment.attemptId).routeId)
    if plan.route.legacyUnverified:
        raise WorkflowConflict("legacy unverified route cannot be executed")
    store.resolve_segment_start(segment_id)
    try:
        store._verify_plan_compiles(plan)
    except Exception as exc:
        raise WorkflowConflict(f"route compatibility verification failed: {exc}") from exc
    project = build_segment_project(store, segment_id)
    normalized = project.model_dump(mode="json", exclude_none=True)
    started = _start_run(supervisor, normalized)
    run_id = started["runId"]
    store.mark_segment_started(segment_id, run_id, full_project_hash(project))
    thread = threading.Thread(target=_monitor, args=(store, supervisor, segment_id, run_id), daemon=True)
    thread.start()
    return store.get_segment(segment_id), run_id
