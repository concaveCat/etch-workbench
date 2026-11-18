"""Per-engine-pair comparability grades with wildcard support for custom engines."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from engines.protocol import Comparability, StepIR

TABLE_PATH = Path(__file__).resolve().parent / "comparability.json"
DEFAULT: Comparability = "no_counterpart"


@lru_cache(maxsize=1)
def table() -> dict[str, Any]:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def _key(step: StepIR) -> str:
    if step.mechanism == "initial":
        return "initial"
    if step.mechanism == "custom":
        return "*/custom"
    return f"{step.kind}/{step.mechanism}"


def _lookup_pair(pairs: dict[str, Any], left: str, right: str) -> dict[str, Any] | None:
    for candidate in (
        f"{left}|{right}",
        f"{right}|{left}",
        f"*|{right}",
        f"{right}|*",
        f"*|{left}",
        f"{left}|*",
        "*|*",
    ):
        if candidate in pairs:
            return pairs[candidate]
    return None


def grade(engine_ids: list[str] | tuple[str, ...], step: StepIR) -> Comparability:
    """Grade for a step across a set of engines."""
    # Unmapped cutoff-only CMP has no SPX counterpart.
    if step.mechanism == "cmp" and "spx" in engine_ids and not step.parameters.get("stop_material"):
        return "no_counterpart"
    pairs = table()["pairs"]
    key = _key(step)
    grades: list[Comparability] = []
    ids = list(engine_ids)
    if len(ids) == 1:
        entry = _lookup_pair(pairs, ids[0], ids[0])
        return entry.get(key, DEFAULT) if entry else "exact"
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            entry = _lookup_pair(pairs, left, right)
            if entry is None:
                grades.append(DEFAULT)
            else:
                grades.append(entry.get(key, DEFAULT))
    if not grades:
        return DEFAULT
    for level in ("no_counterpart", "approximate", "exact"):
        if level in grades:
            return level  # type: ignore[return-value]
    return DEFAULT
