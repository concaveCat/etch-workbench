"""Worker-step adapter for the native implant field plugin."""

from __future__ import annotations

from typing import Any, Mapping

from .params import ImplantParams

__all__ = ["parse_step"]

_KEYS = (
    "species",
    "material",
    "energyKeV",
    "doseCm2",
    "tiltDeg",
    "rotationDeg",
    "doseControl",
    "profile",
    "damage",
    "maskMaterials",
    "screenMaterials",
    "labels",
    "damageFactor",
    "cellSet",
)


def parse_step(project: Mapping[str, Any], step: Mapping[str, Any]) -> ImplantParams | None:
    """Return validated implant parameters, or ``None`` for another model.

    A step that claims ``model="implant"`` but carries an unsupported profile
    source or a malformed value raises ``ValueError``. It is never silently
    left to a different execution path.
    """
    del project
    if step.get("model") != "implant":
        return None
    payload = {name: step[name] for name in _KEYS if name in step}
    return ImplantParams(**payload)
