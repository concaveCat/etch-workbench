"""Pure verification of Measurement -> Segment execution provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .metrology import MeasurementResultV1, snapshot_content_hash
from .models import CompiledWorkflowV1
from .provenance import (
    EquivalenceEvidenceV1,
    ExecutionEvidenceV1,
    ProvenanceVerificationV1,
    SourceIdentityEvidenceV1,
    TargetStepEvidenceV1,
)
from .routes import AttemptV1, RoutePlanV1, SegmentV1


BINDING_METHOD_VERSION = "binding-v1"


def _step_payload(step: Any) -> dict[str, Any]:
    payload = step.model_dump(mode="json", exclude_none=True)
    payload.pop("name", None)
    return payload


def _segment_slice_reasons(
    *,
    compiled: CompiledWorkflowV1,
    segment: SegmentV1,
    submitted_project: Any | None,
    attempt: AttemptV1,
) -> list[str]:
    """Check that the actual Run project is the expected slice of the compile."""

    reasons: list[str] = []
    if submitted_project is None:
        return ["actual submitted project is unavailable"]
    compile_steps = compiled.project.steps
    step_ids = [step.id for step in compile_steps]
    start = 0 if segment.startAfterStepId is None else (
        step_ids.index(segment.startAfterStepId) + 1 if segment.startAfterStepId in step_ids else -1
    )
    end = step_ids.index(segment.throughStepId) if segment.throughStepId in step_ids else -1
    if start < 0 or end < 0 or start > end:
        return ["Segment range does not select a valid compile slice"]
    expected_steps = compile_steps[start:end + 1]
    actual_steps = list(submitted_project.steps)
    if [step.id for step in actual_steps] != [step.id for step in expected_steps]:
        return ["actual submitted steps do not match the compile slice"]
    if [_step_payload(step) for step in actual_steps] != [_step_payload(step) for step in expected_steps]:
        reasons.append("actual submitted step parameters differ from the compile slice")

    for key in ("dimension", "units", "domain", "geometry", "materialDefinitions", "outputSettings"):
        expected = compiled.project.model_dump(mode="json", exclude_none=True).get(key)
        actual = submitted_project.model_dump(mode="json", exclude_none=True).get(key)
        if expected != actual:
            reasons.append(f"actual submitted {key} differs from the compile binding")

    expected_solver = compiled.project.solver.model_dump(mode="json", exclude_none=True)
    actual_solver = submitted_project.solver.model_dump(mode="json", exclude_none=True)
    for payload in (expected_solver, actual_solver):
        payload.pop("timeoutSeconds", None)
        payload.pop("random", None)
    if expected_solver != actual_solver:
        reasons.append("actual submitted solver settings differ from the compile")
    random = submitted_project.solver.random
    if random.mode != "fixed" or random.seed != attempt.runSeed:
        reasons.append("actual submitted random seed does not match the attempt runSeed")

    if segment.startAfterStepId is None:
        if submitted_project.continuationState is not None:
            reasons.append("initial Segment project unexpectedly has a continuation state")
    else:
        state = submitted_project.continuationState
        if state is None:
            reasons.append("continued Segment project has no continuation state")
        elif segment.startCheckpointRef is not None and state.stateId != segment.startCheckpointRef.stateHash:
            reasons.append("actual continuation state does not match the Segment checkpoint")
    return reasons


def verify_binding(
    *,
    measurement: MeasurementResultV1,
    plan: RoutePlanV1,
    attempt: AttemptV1,
    segment: SegmentV1,
    compile_record: CompiledWorkflowV1,
    manifest: dict[str, Any] | None,
    snapshot_row: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    state_hash: str | None,
    submitted_project: Any | None,
    submitted_project_hash: str | None,
    run_id: str,
    step_id: str,
) -> ProvenanceVerificationV1:
    execution_reasons: list[str] = []
    target_reasons: list[str] = []
    source_reasons: list[str] = []

    manifest = manifest or {}
    row = snapshot_row or {}
    state_id = (row.get("nativeState") or {}).get("stateId")
    recomputed_snapshot_hash = snapshot_content_hash(snapshot) if snapshot is not None else None

    if run_id != segment.runId:
        execution_reasons.append("requested runId does not match Segment.runId")
    if manifest.get("runId") != run_id:
        execution_reasons.append("run manifest does not belong to the requested run")
    if segment.sourceCompileId != compile_record.compileRecord.compileId:
        execution_reasons.append("Segment sourceCompileId does not match the compile record")
    if segment.flowDigest != compile_record.compileRecord.fullProjectHash:
        execution_reasons.append("Segment flowDigest does not match the compile record")
    if attempt.flowDigest != compile_record.compileRecord.fullProjectHash:
        execution_reasons.append("Attempt flowDigest does not match the compile record")
    if submitted_project_hash is None:
        execution_reasons.append("actual submitted project is unavailable")
    elif segment.segmentProjectHash != submitted_project_hash:
        execution_reasons.append("actual submitted project does not match segmentProjectHash")
    execution_reasons.extend(
        _segment_slice_reasons(
            compiled=compile_record,
            segment=segment,
            submitted_project=submitted_project,
            attempt=attempt,
        )
    )
    if manifest.get("status") not in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
        execution_reasons.append("run manifest is not terminal")

    step_index = None
    if step_id in plan.stepOrder:
        step_index = plan.stepOrder.index(step_id)
    start = 0 if segment.startAfterStepId is None else (
        plan.stepOrder.index(segment.startAfterStepId) + 1 if segment.startAfterStepId in plan.stepOrder else -1
    )
    end = plan.stepOrder.index(segment.throughStepId) if segment.throughStepId in plan.stepOrder else -1
    if step_index is None or start < 0 or end < 0 or not (start <= step_index <= end):
        target_reasons.append("target step is outside the Segment step range")
    if step_id not in {entry.stepId for entry in compile_record.sourceMap}:
        target_reasons.append("target step is not present in the source compile")
    if row.get("stepId") != step_id:
        target_reasons.append("run manifest has no snapshot row for the target step")
    completed = bool(row) and bool(row.get("snapshot") or row.get("vtk")) and row.get("stepIndex") is not None
    if not completed:
        target_reasons.append("target step is not recorded as completed")
    if recomputed_snapshot_hash is None:
        target_reasons.append("target snapshot cannot be read")
    if state_id is None:
        target_reasons.append("target snapshot has no native state")

    if measurement.sourceRunId != run_id:
        source_reasons.append("measurement sourceRunId does not match the requested run")
    if measurement.sourceStepId != step_id:
        source_reasons.append("measurement sourceStepId does not match the target step")
    if measurement.sourceStateId != state_id:
        source_reasons.append("measurement sourceStateId does not match the manifest state")
    if measurement.sourceSnapshotHash != recomputed_snapshot_hash:
        source_reasons.append("sourceSnapshotHash does not match the snapshot actually read")
    if state_hash is None or state_id is None or state_hash != state_id:
        source_reasons.append("native state file hash does not match sourceStateId")

    execution = ExecutionEvidenceV1(
        status="verified" if not execution_reasons else "failed",
        segmentId=segment.segmentId,
        runId=run_id,
        sourceCompileId=segment.sourceCompileId,
        segmentProjectHash=segment.segmentProjectHash,
        submittedProjectHash=submitted_project_hash,
        manifestRunId=manifest.get("runId"),
        reasons=execution_reasons,
    )
    target = TargetStepEvidenceV1(
        status="verified" if not target_reasons else "failed",
        stepId=step_id,
        stepIndex=row.get("stepIndex"),
        completed=completed,
        snapshotFile=row.get("snapshot"),
        snapshotHash=recomputed_snapshot_hash,
        stateId=state_id,
        stateHash=state_hash,
        reasons=target_reasons,
    )
    source = SourceIdentityEvidenceV1(
        status="verified" if not source_reasons else "failed",
        measurementStateId=measurement.sourceStateId,
        manifestStateId=state_id,
        recomputedSnapshotHash=recomputed_snapshot_hash,
        measurementSnapshotHash=measurement.sourceSnapshotHash,
        reasons=source_reasons,
    )
    evidence = ProvenanceVerificationV1(
        methodVersion=BINDING_METHOD_VERSION,
        verifiedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        execution=execution,
        targetStep=target,
        sourceIdentity=source,
        equivalence=EquivalenceEvidenceV1(status="not_evaluated"),
    )
    return evidence
