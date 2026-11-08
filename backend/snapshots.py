"""Materialise deferred step snapshots on first read.

The worker keeps step snapshots out of the run's critical path: it writes
``<step>.meta.json`` only, plus the native ``state-<step>.vpsd``. The full
snapshot document is regenerated here (in a bounded child, so the API process
never imports ViennaPS) and then cached as ``<step>.json``.

The regenerated document is byte-identical to what the eager writer produced:
both call ``worker.simulate.save_snapshot`` with the same project and Domain
state, and ``worker/materialize_snapshot.py`` re-checks the point/cell counts
against the recorded metadata.

Regeneration happens in an isolated staging directory, not in the run
directory. ``worker.simulate.save_snapshot`` enforces the application cap
(``backend.runtime_limits``, default 4096 MiB) on whatever directory it writes,
so regenerating in place made a deferred snapshot unreadable for every run that
had already reached the cap through its own native states -- a run that
succeeded could never serve its final snapshot. Staging keeps the budget
meaningful for the *new* files (the snapshot plus the native state it is
derived from must fit under the cap, because the state is staged by reference
and counted) without re-charging the budget of a run that is already finished.
The produced files are then moved into the run directory with ``os.replace`` so
the existing atomic-write and per-run-lock guarantees are preserved.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import storage
from .engine import version_mismatch
from .runtime_limits import artifact_limit_mib

ROOT = storage.ROOT
PYTHON = ROOT / ".venv" / "bin" / "python"
GUARD = ROOT / "scripts" / "bounded_child.py"
WORKER = ROOT / "worker" / "materialize_snapshot.py"
# Inside the workbench data directory: the staging tree must live on the same
# filesystem as ``var/runs`` so publishing is a rename, not a copy, and
# ``scripts/bounded_child.py`` only supervises commands rooted at the workbench.
STAGING_ROOT = storage.VAR / "snapshot-staging"
# A staging tree only outlives its process if the API is killed mid-read; do
# not let such trees accumulate silently in the data directory.
STALE_STAGING_SECONDS = 24 * 3600


def _prune_stale_staging() -> None:
    cutoff = time.time() - STALE_STAGING_SECONDS
    try:
        entries = list(STAGING_ROOT.iterdir())
    except OSError:
        return
    for item in entries:
        try:
            if item.is_dir() and not item.is_symlink() and item.stat().st_mtime < cutoff:
                shutil.rmtree(item, ignore_errors=True)
        except OSError:
            continue


def _stage(run_dir: Path, step_id: str, staging: Path) -> None:
    """Link the read-only inputs into ``staging`` under their run-dir names.

    ``materialize_snapshot.py`` expects ``<step>.meta.json`` and
    ``state-<step>.vpsd`` next to the directory it writes to. Linking instead
    of copying keeps the staging footprint at the size of the regenerated
    snapshot; the worker still counts the linked state against the budget
    because ``Path.stat`` follows the link.
    """
    names = (f"{step_id}.meta.json", f"state-{step_id}.vpsd")
    for name in names:
        source = run_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"snapshot {step_id} is not available: {name} is missing")
        (staging / name).symlink_to(source.resolve())


def _publish(staging: Path, run_dir: Path, step_id: str) -> Path:
    """Move every produced file into ``run_dir``; return the snapshot JSON."""
    for item in sorted(staging.rglob("*")):
        if item.is_symlink() or not item.is_file() or item.name.endswith(".tmp"):
            continue
        relative = item.relative_to(staging)
        destination = run_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(item, destination)
    return run_dir / f"{step_id}.json"


def ensure_materialized(run_dir: Path, step_id: str) -> Path:
    """Return ``<step>.json``, regenerating it from ``.vpsd`` when deferred."""
    target = run_dir / f"{step_id}.json"
    if target.is_file():
        return target
    if not (run_dir / f"{step_id}.meta.json").is_file():
        raise FileNotFoundError(f"snapshot {step_id} is not available")
    input_path = run_dir / "input.json"
    if not input_path.is_file():
        raise FileNotFoundError("run input is missing")
    config = storage.read_json(input_path)
    mismatch = version_mismatch(config.get("expectedVersions") if isinstance(config, dict) else None)
    if mismatch is not None:
        # Refuse before staging or spawning a worker: a deferred snapshot is
        # still a native read of the original run's Domain state.
        raise mismatch

    # One materialisation per run at a time: the JSON write itself is atomic,
    # but the auxiliary artifacts written by save_snapshot are not.
    with (run_dir / ".snapshot-materialize.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_file():
            return target
        STAGING_ROOT.mkdir(parents=True, exist_ok=True)
        _prune_stale_staging()
        staging = Path(tempfile.mkdtemp(prefix=f"{step_id}.staging-", dir=STAGING_ROOT))
        record = run_dir / f".materialize-{step_id}.guard.json"
        log = run_dir / f".materialize-{step_id}.log"
        try:
            _stage(run_dir, step_id, staging)
            command = [str(PYTHON), str(GUARD), "--cwd", str(ROOT),
                       "--record", str(record), "--log-path", str(log),
                       "--timeout-seconds", "180", "--max-rss-mib", "4096",
                       "--max-log-bytes", "200000", "--",
                       str(PYTHON), str(WORKER), str(input_path), str(staging), step_id]
            try:
                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                           timeout=200, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"snapshot materialisation failed: {type(exc).__name__}: {exc}") from exc
            if completed.returncode != 0 or not (staging / f"{step_id}.json").is_file():
                detail = ""
                if log.is_file():
                    detail = log.read_text(encoding="utf-8", errors="replace")[-800:]
                detail = detail or (completed.stderr or "")[-800:]
                message = f"snapshot materialisation failed: {detail.strip()}"
                if "application cap" in detail:
                    message = (f"{message}\nThe regenerated snapshot does not fit the "
                               f"{artifact_limit_mib()} MiB artifact cap. It is staged under "
                               f"{STAGING_ROOT} and the snapshot plus its native state must fit on "
                               f"their own, because the finished run is already over the cap.")
                raise RuntimeError(message)
            target = _publish(staging, run_dir, step_id)
            if not target.is_file():
                raise RuntimeError("snapshot materialisation produced no snapshot document")
            (run_dir / f"{step_id}.meta.json").unlink(missing_ok=True)
            record.unlink(missing_ok=True)
            log.unlink(missing_ok=True)
            return target
        finally:
            shutil.rmtree(staging, ignore_errors=True)
