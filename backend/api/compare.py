"""Comparison runs: one compiled StepIR flow, several engines, one aligned record.

The comparison layer is intentionally separate from the existing run supervisor.
A comparison run never touches ``var/runs``: it owns ``var/compare-runs/<id>`` and
is driven by :mod:`engines.scheduler`, which spawns one subprocess per engine.

Routes (all loopback-only, like the rest of the service):

* ``GET  /api/compare/engines``           - registered engines and their claims
* ``POST /api/compare/runs``              - submit ``{job, engines}``, returns a run id
* ``GET  /api/compare/runs``              - newest comparisons with their status
* ``GET  /api/compare/runs/{run_id}``     - status, ``compare.json`` when finished
* ``GET  /api/compare/runs/{run_id}/frames/{path}`` - one frame file
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from .. import storage

COMPARE_RUNS = storage.VAR / "compare-runs"
STATUS_FILE = "status.json"
_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()


def _invalid(path: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=[{"path": path, "message": message}])


def _new_id() -> str:
    return "%024x" % int.from_bytes(__import__("os").urandom(12), "big")


def _run_dir(run_id: str) -> Path:
    if not storage.ID_RE.fullmatch(run_id or ""):
        raise HTTPException(status_code=404, detail="comparison run not found")
    path = COMPARE_RUNS / run_id
    if path.is_dir():
        return path
    raise HTTPException(status_code=404, detail="comparison run not found")


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    with _lock_guard:
        lock = _locks.setdefault(str(path), threading.Lock())
    with lock:
        storage.atomic_json(path, payload)


def _read_status(path: Path) -> dict[str, Any] | None:
    try:
        return storage.read_json(path)
    except (OSError, ValueError):
        return None


def _execute(run_id: str, job_payload: dict[str, Any], engine_ids: list[str],
             timeout: float) -> None:
    from engines.protocol import JobSpec, ProtocolError
    from engines.scheduler import compare

    run_dir = COMPARE_RUNS / run_id
    status_path = run_dir / STATUS_FILE
    try:
        job = JobSpec.from_json(job_payload)
    except (ProtocolError, KeyError, TypeError, ValueError) as exc:
        _write_status(status_path, {"runId": run_id, "state": "rejected",
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "updatedAt": time.time()})
        return
    _write_status(status_path, {"runId": run_id, "state": "running",
                                "caseId": job.case_id, "engines": engine_ids,
                                "startedAt": time.time()})
    try:
        payload = compare(job, engine_ids, run_dir / "out", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the status file is the report
        _write_status(status_path, {"runId": run_id, "state": "failed",
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "updatedAt": time.time()})
        return
    _write_status(status_path, {"runId": run_id, "state": "succeeded",
                                "caseId": job.case_id, "engines": engine_ids,
                                "summary": payload["summary"],
                                "updatedAt": time.time()})


def build_router() -> APIRouter:
    # ``include_in_schema=False``: ``docs/workflow-openapi.json`` is the frozen
    # workflow contract and several tests assert exact equality against it. The
    # comparison surface is documented in docs/engine-abstraction.md instead.
    router = APIRouter(prefix="/api/compare", tags=["compare"], include_in_schema=False)

    @router.get("/engines")
    async def engines() -> dict[str, Any]:
        from engines.registry import describe

        return {"engines": describe()}

    @router.post("/runs")
    async def create_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        job = payload.get("job")
        if not isinstance(job, dict):
            raise _invalid("job", "job must be an object")
        engine_ids = payload.get("engines") or ["viennaps", "spx"]
        if not isinstance(engine_ids, list) or not all(isinstance(v, str) for v in engine_ids):
            raise _invalid("engines", "engines must be a list of engine ids")
        from engines.protocol import JobSpec, ProtocolError
        from engines.registry import registrations

        try:
            JobSpec.from_json(job).validate()
        except (ProtocolError, KeyError, TypeError, ValueError) as exc:
            raise _invalid("job", f"invalid job: {exc}") from exc
        known = registrations()
        unknown = [value for value in engine_ids if value not in known]
        if unknown:
            raise _invalid("engines", f"unknown engine(s) {unknown}; known: {sorted(known)}")

        run_id = _new_id()
        run_dir = COMPARE_RUNS / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_status(run_dir / STATUS_FILE, {"runId": run_id, "state": "queued",
                                              "caseId": job.get("case_id"),
                                              "engines": engine_ids,
                                              "queuedAt": time.time()})
        thread = threading.Thread(
            target=_execute,
            args=(run_id, job, engine_ids, float(payload.get("timeout", 1800.0))),
            daemon=True, name=f"compare-{run_id}",
        )
        thread.start()
        return {"runId": run_id, "state": "queued"}

    @router.get("/runs")
    async def list_runs(limit: int = 20) -> dict[str, Any]:
        if not COMPARE_RUNS.is_dir():
            return {"runs": []}
        rows = []
        for path in sorted(COMPARE_RUNS.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            status = _read_status(path / STATUS_FILE)
            if status is None:
                continue
            rows.append(status)
            if len(rows) >= max(1, min(limit, 100)):
                break
        return {"runs": rows}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        run_dir = _run_dir(run_id)
        status = _read_status(run_dir / STATUS_FILE) or {"runId": run_id, "state": "unknown"}
        compare_path = run_dir / "out" / "compare.json"
        if compare_path.is_file():
            status = {**status, "compare": storage.read_json(compare_path)}
        return status

    @router.get("/runs/{run_id}/frames/{frame_path:path}")
    async def get_frame(run_id: str, frame_path: str) -> FileResponse:
        run_dir = _run_dir(run_id).resolve()
        target = (run_dir / "out" / frame_path).resolve()
        if run_dir not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="frame not found")
        if target.suffix not in (".json", ".png"):
            raise HTTPException(status_code=415, detail="unsupported frame type")
        return FileResponse(target)

    return router


router = build_router()
