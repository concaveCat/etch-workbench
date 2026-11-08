"""Read the v2 execution records under a run directory.

The v2 executor writes ``<run>/v2/<root>/steps/<step>/`` with plan, state, and
outputs documents. This module exposes the small JSON half to run inspection
and replay/UI clients; dense field arrays stay in ``fields.npz``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["read_v2_steps", "V2_RECORDS_VERSION"]


V2_RECORDS_VERSION = 1


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_v2_steps(run_dir: Path) -> list[dict[str, Any]]:
    """Return one JSON-safe record per v2 step, sorted by step id."""
    root = Path(run_dir) / "v2"
    if not root.is_dir():
        return []
    records: dict[str, dict[str, Any]] = {}
    for execution_root in root.iterdir():
        if not execution_root.is_dir():
            continue
        steps_dir = execution_root / "steps"
        if not steps_dir.is_dir():
            continue
        for step_dir in steps_dir.iterdir():
            if not step_dir.is_dir():
                continue
            plan = _read(step_dir / "plan.json")
            outputs = _read(step_dir / "outputs.json")
            payload = _read(step_dir / "state.json")
            state = None
            if isinstance(payload, dict):
                state = {
                    "materials": payload.get("materials"),
                    "geometry": payload.get("geometry"),
                    "layers": payload.get("layers"),
                    "fieldsOmitted": bool(payload.get("fieldsOmitted")),
                    "hasFields": payload.get("fields") is not None,
                    "fieldsPersisted": (step_dir / "fields.npz").is_file(),
                }
            records[step_dir.name] = {
                "stepId": step_dir.name,
                "plan": plan,
                "outputs": outputs,
                "state": state,
            }
    return [records[key] for key in sorted(records)]
