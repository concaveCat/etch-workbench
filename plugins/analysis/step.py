"""Worker-step adapter for the native cell-set analysis plugin."""

from __future__ import annotations

from typing import Any, Mapping

from .params import AnalysisParams

__all__ = ["parse_step"]

_KEYS = ("netDoping", "sheetResistance", "scanDepth", "cellSet")


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> AnalysisParams | None:
    """Return validated analysis parameters, or ``None`` for another model."""
    del project
    if step.get("model") != "analysis":
        return None
    payload = {name: step[name] for name in _KEYS if name in step}
    return AnalysisParams(**payload)
