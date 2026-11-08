"""Atomic artifact writers and per-run artifact accounting for the worker.

These helpers belong to the bounded child, not the API process: the worker
writes compact JSON, hashes the files it commits and re-checks the run budget
after every write. ``backend.storage`` keeps the API-side equivalents.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from backend.runtime_limits import artifact_limit_bytes, artifact_limit_label


def atomic_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Snapshot JSON contains large point/cell arrays. Compact serialization
    # avoids spending time on indentation and reduces per-run artifact size.
    # Keep the encode-then-write form: measured on a 7.4 MB snapshot,
    # json.dump(stream) is ~3.8x slower than json.dumps() + write_text()
    # (293 ms vs 77 ms), because the stream path writes in small chunks.
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def check_artifact_budget(run_dir: Path) -> None:
    max_run_bytes = artifact_limit_bytes()
    total = 0
    for item in run_dir.rglob("*"):
        try:
            # The supervisor atomically replaces manifest.json while this
            # bounded worker writes snapshots. A temporary manifest can
            # disappear between is_file() and stat(); temporary files are not
            # retained artifacts and must not count against the run budget.
            if item.name.endswith(".tmp") or not item.is_file():
                continue
            total += item.stat().st_size
            if total > max_run_bytes:
                raise RuntimeError(f"run artifacts exceeded the {artifact_limit_label()} application cap")
        except OSError:
            # A concurrent atomic replace may remove a file after rglob has
            # yielded its path. Continue with the remaining committed files.
            continue


def artifact_bytes_used(run_dir: Path) -> int:
    """Return the committed run payload without raising on a transient file."""
    total = 0
    for item in run_dir.rglob("*"):
        try:
            if item.name.endswith(".tmp") or not item.is_file():
                continue
            total += item.stat().st_size
        except OSError:
            continue
    return total


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def output_artifact(
    path: Path, run_dir: Path, run_id: str, step_id: str, kind: str, fmt: str, **details: Any
) -> dict[str, Any]:
    relative = path.resolve().relative_to(run_dir.resolve()).as_posix()
    return {
        "kind": kind,
        "fileName": relative,
        "url": f"/api/runs/{run_id}/artifacts/{step_id}?fileName={relative}",
        "format": fmt,
        "sizeBytes": path.stat().st_size,
        "sha256": file_sha256(path),
        **details,
    }
