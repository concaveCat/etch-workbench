"""Map a raw worker step to this process's parameters.

``parse_step`` is the ``ProcessSpec`` hook: return params for a step that
belongs to this process, ``None`` for another process, and raise ``ValueError``
for a malformed step that claims to be this process.
"""

from __future__ import annotations

from typing import Any, Mapping

from .params import ExampleParams

__all__ = ["parse_step"]


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> ExampleParams | None:
    del project
    if step.get("model") != "example":
        return None
    if step.get("operation") != "etch":
        return None

    rate = step.get("rate")
    duration = step.get("duration")
    if rate is None or duration is None:
        raise ValueError("example step requires rate and duration")
    return ExampleParams(rate=float(rate), duration=float(duration))
