"""Worker-step adapter for the native anneal field plugin."""

from __future__ import annotations

from typing import Any, Mapping

from .params import AnnealParams

__all__ = ["parse_step"]

_KEYS = (
    "mode",
    "schedule",
    "diffusivity",
    "timeStep",
    "stabilityFactor",
    "implicitSolver",
    "diffusionMaterials",
    "blockingMaterials",
    "speciesLabel",
    "activeLabel",
    "solidActivation",
    "activationOnly",
    "cellSet",
)


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> AnnealParams | None:
    """Return validated anneal parameters, or ``None`` for another model."""
    del project
    if step.get("model") != "anneal":
        return None
    payload = {name: step[name] for name in _KEYS if name in step}
    return AnnealParams(**payload)
