"""Canonical parameters for the native silicon thermal-oxidation plugin.

The flat fields and their defaults/ranges mirror
``backend.specialized_models`` for the ``oxidation`` catalog row. They are
repeated here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer).

There is deliberately no ``duration`` field: the native ``ps.Oxidation`` model
carries its own ``timeHours``/``timeStepHours`` controls, and the worker derives
the ``ps.Process`` duration as ``timeHours * 3600`` seconds. A step for this
process therefore may omit ``duration`` entirely.

``maskMaterial`` is present with the catalog default ``""`` so the parameter
model matches the project schema, but this plugin implements the no-mask path
only. :mod:`plugins.oxidation.step` declines any step that sets it, leaving the
step to the old worker path (mask mechanics is not verified on this host).
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FLAT_FIELDS",
    "MATERIAL_FIELDS",
    "NUMERIC_FIELDS",
    "OxidationParams",
]

#: Raw ``nativeParameters`` keys in catalog order.
FLAT_FIELDS: tuple[str, ...] = (
    "oxidant",
    "orientation",
    "temperatureC",
    "pressureAtm",
    "timeHours",
    "timeStepHours",
    "transferCoefficientUmHr",
    "initialOxideThicknessUm",
    "reactionActivationVolumeM3",
    "diffusionActivationVolumeM3",
    "maskMaterial",
    "siliconMaterial",
    "oxideMaterial",
)

#: Fields that reach a ``float`` setter on ``ps.Oxidation``.
NUMERIC_FIELDS: tuple[str, ...] = (
    "temperatureC",
    "pressureAtm",
    "timeHours",
    "timeStepHours",
    "transferCoefficientUmHr",
    "initialOxideThicknessUm",
    "reactionActivationVolumeM3",
    "diffusionActivationVolumeM3",
)

#: Fields that name a material in the installed native registry.
MATERIAL_FIELDS: tuple[str, ...] = ("maskMaterial", "siliconMaterial", "oxideMaterial")

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, Any] = {
    "oxidant": "Dry",
    "orientation": "Si100",
    "temperatureC": 1000.0,
    "pressureAtm": 1.0,
    "timeHours": 0.001,
    "timeStepHours": 0.0001,
    "transferCoefficientUmHr": 0.1,
    "initialOxideThicknessUm": 0.0,
    "reactionActivationVolumeM3": 0.0,
    "diffusionActivationVolumeM3": 0.0,
    "maskMaterial": "",
    "siliconMaterial": "Si",
    "oxideMaterial": "SiO2",
}


class OxidationParams(BaseModel):
    """Parameters of one native ``ps.Oxidation`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by ``backend.validation`` is accepted here and vice versa.
    ``timeHours`` and ``timeStepHours`` are additionally strictly positive.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    oxidant: Literal["Dry", "Wet"] = _DEFAULTS["oxidant"]
    orientation: Literal["Si100", "Si110", "Si111", "PolySi"] = _DEFAULTS["orientation"]
    temperatureC: float = Field(default=_DEFAULTS["temperatureC"], ge=800.0, le=1200.0)
    pressureAtm: float = Field(default=_DEFAULTS["pressureAtm"], ge=0.000001, le=_MAXIMUM)
    timeHours: float = Field(default=_DEFAULTS["timeHours"], ge=0.000003, le=0.1666667)
    timeStepHours: float = Field(default=_DEFAULTS["timeStepHours"], ge=0.000001, le=_MAXIMUM)
    transferCoefficientUmHr: float = Field(default=_DEFAULTS["transferCoefficientUmHr"], ge=0.0, le=_MAXIMUM)
    initialOxideThicknessUm: float = Field(default=_DEFAULTS["initialOxideThicknessUm"], ge=0.0, le=_MAXIMUM)
    reactionActivationVolumeM3: float = Field(default=_DEFAULTS["reactionActivationVolumeM3"], ge=0.0, le=_MAXIMUM)
    diffusionActivationVolumeM3: float = Field(default=_DEFAULTS["diffusionActivationVolumeM3"], ge=0.0, le=_MAXIMUM)
    maskMaterial: str = _DEFAULTS["maskMaterial"]
    siliconMaterial: str = Field(default=_DEFAULTS["siliconMaterial"], min_length=1)
    oxideMaterial: str = Field(default=_DEFAULTS["oxideMaterial"], min_length=1)

    @field_validator(*NUMERIC_FIELDS)
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @property
    def duration_seconds(self) -> float:
        """Return the ``ps.Process`` duration the worker derives for this step."""
        return float(self.timeHours) * 3600.0

    def native_values(self) -> dict[str, Any]:
        """Return every flat value keyed by its ``nativeParameters`` name."""
        return {
            name: (float(getattr(self, name)) if name in NUMERIC_FIELDS else str(getattr(self, name)))
            for name in FLAT_FIELDS
        }
