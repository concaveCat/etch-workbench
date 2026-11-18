"""Comparison runs: one compiled StepIR flow, N engines, one aligned benchmark record."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from .. import storage

COMPARE_RUNS = storage.VAR / "compare-runs"
STATUS_FILE = "status.json"
ADOPTED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()


def _compare_root(run_id: str, *, must_exist: bool = True) -> tuple[Path, Path | None]:
    if storage.ID_RE.fullmatch(run_id or ""):
        run_dir = COMPARE_RUNS / run_id
        if not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="comparison run not found")
        return run_dir / "out", run_dir / STATUS_FILE
    if ADOPTED_RE.fullmatch(run_id or ""):
        candidate = (storage.VAR / run_id).resolve()
        if (
            storage.VAR.resolve() in candidate.parents
            and (candidate / "compare.json").is_file()
        ):
            return candidate, None
    del must_exist
    raise HTTPException(status_code=404, detail="comparison run not found")


def _invalid(path: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=[{"path": path, "message": message}])


def _new_id() -> str:
    return "%024x" % int.from_bytes(os.urandom(12), "big")


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


def _execute(
    run_id: str,
    job_payload: dict[str, Any],
    engine_ids: list[str],
    timeout: float,
    reference: str | None,
    sequential: bool,
    probe_points: int,
) -> None:
    from engines.protocol import JobSpec, ProtocolError
    from engines.scheduler import compare

    run_dir = COMPARE_RUNS / run_id
    status_path = run_dir / STATUS_FILE
    try:
        job = JobSpec.from_json(job_payload)
    except (ProtocolError, KeyError, TypeError, ValueError) as exc:
        _write_status(
            status_path,
            {
                "runId": run_id,
                "state": "rejected",
                "detail": f"{type(exc).__name__}: {exc}",
                "updatedAt": time.time(),
            },
        )
        return
    _write_status(
        status_path,
        {
            "runId": run_id,
            "state": "running",
            "caseId": job.case_id,
            "engines": engine_ids,
            "startedAt": time.time(),
        },
    )
    try:
        payload = compare(
            job,
            engine_ids,
            run_dir / "out",
            timeout=timeout,
            max_probe_points=probe_points,
            reference=reference,
            parallel=not sequential,
        )
    except Exception as exc:  # noqa: BLE001
        _write_status(
            status_path,
            {
                "runId": run_id,
                "state": "failed",
                "detail": f"{type(exc).__name__}: {exc}",
                "updatedAt": time.time(),
            },
        )
        return
    _write_status(
        status_path,
        {
            "runId": run_id,
            "state": "succeeded",
            "caseId": job.case_id,
            "engines": engine_ids,
            "summary": payload["summary"],
            "updatedAt": time.time(),
        },
    )


def build_router() -> APIRouter:
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
        reference = payload.get("reference")
        if reference is not None and reference not in engine_ids:
            raise _invalid("reference", f"reference engine {reference!r} must be in engines")

        run_id = _new_id()
        run_dir = COMPARE_RUNS / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_status(
            run_dir / STATUS_FILE,
            {
                "runId": run_id,
                "state": "queued",
                "caseId": job.get("case_id"),
                "engines": engine_ids,
                "queuedAt": time.time(),
            },
        )
        thread = threading.Thread(
            target=_execute,
            args=(
                run_id,
                job,
                engine_ids,
                float(payload.get("timeout", 1800.0)),
                str(reference) if reference else None,
                bool(payload.get("sequential", False)),
                int(payload.get("probePoints", 3000)),
            ),
            daemon=True,
            name=f"compare-{run_id}",
        )
        thread.start()
        return {"runId": run_id, "state": "queued"}

    @router.get("/runs")
    async def list_runs(limit: int = 20) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        if COMPARE_RUNS.is_dir():
            for path in sorted(COMPARE_RUNS.iterdir(), reverse=True):
                if not path.is_dir():
                    continue
                status = _read_status(path / STATUS_FILE)
                if status is not None:
                    rows.append(status)
        if storage.VAR.is_dir():
            for path in sorted(storage.VAR.iterdir(), reverse=True):
                if not path.is_dir() or not ADOPTED_RE.fullmatch(path.name):
                    continue
                if not (path / "compare.json").is_file():
                    continue
                rows.append(
                    {
                        "runId": path.name,
                        "state": "succeeded",
                        "caseId": path.name,
                        "adopted": True,
                        "updatedAt": (path / "compare.json").stat().st_mtime,
                    }
                )
        rows.sort(key=lambda row: row.get("updatedAt") or 0, reverse=True)
        return {"runs": rows[: max(1, min(limit, 100))]}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        root, status_path = _compare_root(run_id)
        status = (_read_status(status_path) if status_path else None) or {
            "runId": run_id,
            "state": "succeeded" if (root / "compare.json").is_file() else "unknown",
        }
        compare_path = root / "compare.json"
        if compare_path.is_file():
            status = {**status, "compare": storage.read_json(compare_path)}
        return status

    @router.get("/runs/{run_id}/frames/{frame_path:path}")
    async def get_frame(run_id: str, frame_path: str) -> FileResponse:
        root = _compare_root(run_id)[0].resolve()
        target = (root / frame_path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="frame not found")
        if target.suffix not in (".json", ".png"):
            raise HTTPException(status_code=415, detail="unsupported frame type")
        return FileResponse(target)

    return router


router = build_router()
