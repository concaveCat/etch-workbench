"""Canonical parameters for the native TEOS PECVD plugin.

The eight flat fields and their defaults/ranges mirror
``backend.specialized_models`` for the ``teospecvd`` catalog row. They are
repeated here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer).

The native model always deposits SiO2, so the step has no ``depositMaterial``
parameter and no coverage tolerance: the flat eight-argument constructor is the
whole parameter surface.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["FLAT_FIELDS", "TEOSPECVDParams"]

#: Native constructor argument order of ``ps.TEOSPECVD``.
FLAT_FIELDS: tuple[str, ...] = (
    "stickingProbabilityRadical",
    "depositionRateRadical",
    "depositionRateIon",
    "exponentIon",
    "stickingProbabilityIon",
    "reactionOrderRadical",
    "reactionOrderIon",
    "minAngleIon",
)

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "stickingProbabilityRadical": 0.5,
    "depositionRateRadical": 0.05,
    "depositionRateIon": 0.02,
    "exponentIon": 1.0,
    "stickingProbabilityIon": 1.0,
    "reactionOrderRadical": 1.0,
    "reactionOrderIon": 1.0,
    "minAngleIon": 0.0,
}

#: Sticking probabilities are fractions; the angle is a degree value. All
#: other fields are non-negative rates/orders with the catalog's 1e6 ceiling.
_MAXIMUMS: dict[str, float] = {name: _MAXIMUM for name in FLAT_FIELDS}
_MAXIMUMS["stickingProbabilityRadical"] = 1.0
_MAXIMUMS["stickingProbabilityIon"] = 1.0
_MAXIMUMS["minAngleIon"] = 180.0


class TEOSPECVDParams(BaseModel):
    """Parameters of one native ``ps.TEOSPECVD`` step.

    Every numeric field must be finite; the ranges match the catalog so a step
    accepted by ``backend.validation`` is accepted here and vice versa. The
    native model always deposits SiO2; there is no material or tolerance field.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    stickingProbabilityRadical: float = Field(
        default=_DEFAULTS["stickingProbabilityRadical"],
        ge=0.0,
        le=_MAXIMUMS["stickingProbabilityRadical"],
    )
    depositionRateRadical: float = Field(
        default=_DEFAULTS["depositionRateRadical"],
        ge=0.0,
        le=_MAXIMUMS["depositionRateRadical"],
    )
    depositionRateIon: float = Field(
        default=_DEFAULTS["depositionRateIon"],
        ge=0.0,
        le=_MAXIMUMS["depositionRateIon"],
    )
    exponentIon: float = Field(
        default=_DEFAULTS["exponentIon"],
        ge=0.0,
        le=_MAXIMUMS["exponentIon"],
    )
    stickingProbabilityIon: float = Field(
        default=_DEFAULTS["stickingProbabilityIon"],
        ge=0.0,
        le=_MAXIMUMS["stickingProbabilityIon"],
    )
    reactionOrderRadical: float = Field(
        default=_DEFAULTS["reactionOrderRadical"],
        ge=0.0,
        le=_MAXIMUMS["reactionOrderRadical"],
    )
    reactionOrderIon: float = Field(
        default=_DEFAULTS["reactionOrderIon"],
        ge=0.0,
        le=_MAXIMUMS["reactionOrderIon"],
    )
    minAngleIon: float = Field(
        default=_DEFAULTS["minAngleIon"],
        ge=0.0,
        le=_MAXIMUMS["minAngleIon"],
    )

    @field_validator(*FLAT_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    def flat_arguments(self) -> tuple[float, ...]:
        """Return the eight values in ``ps.TEOSPECVD`` argument order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def flat_values(self) -> dict[str, float]:
        """Return the eight values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}
