"""Per-engine-pair comparability grades.

``engines/comparability.json`` is the single editable table; this module resolves
the grade for a concrete step on a concrete engine pair. The scheduler records the
resolved value in ``compare.json`` so the UI never has to guess, and a grade change
never requires touching a flow.
"""

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
    return f"{step.kind}/{step.mechanism}"


def grade(engine_ids: list[str] | tuple[str, ...], step: StepIR) -> Comparability:
    """Grade for a step across a set of engines.

    With more than two engines the result is the worst grade any pair would get,
    because a chain is only as comparable as its least comparable link.
    """
    pairs = table()["pairs"]
    key = _key(step)
    grades: list[Comparability] = []
    ids = list(engine_ids)
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            entry = pairs.get(f"{left}|{right}") or pairs.get(f"{right}|{left}")
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
