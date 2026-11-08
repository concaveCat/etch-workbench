"""Canonical parameters for the native oxide-regrowth plugin.

The twelve flat fields and their defaults/ranges mirror
``backend.specialized_models`` for the ``oxideRegrowth`` catalog row. They are
repeated here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer). The
bounds are the same ones ``backend.validation`` enforces on
``nativeParameters``: ``0 <= value <= 1e6``.

There is deliberately no ``timeHours`` field: unlike ``Oxidation``, native
``ps.OxideRegrowth`` has no internal time control, so the worker derives the
``ps.Process`` duration from the step's ``duration``. This plugin requires an
explicit positive ``duration`` for the same reason.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["FLAT_FIELDS", "NUMERIC_FIELDS", "OxideRegrowthParams"]

#: Native constructor argument order of ``ps.OxideRegrowth``.
FLAT_FIELDS: tuple[str, ...] = (
    "nitrideEtchRate",
    "oxideEtchRate",
    "redepositionRate",
    "redepositionThreshold",
    "redepositionTimeInt",
    "diffusionCoefficient",
    "sinkStrength",
    "scallopVelocity",
    "centerVelocity",
    "topHeight",
    "centerWidth",
    "stabilityFactor",
)

#: Every flat field reaches a ``float`` constructor argument.
NUMERIC_FIELDS: tuple[str, ...] = FLAT_FIELDS

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "nitrideEtchRate": 0.01,
    "oxideEtchRate": 0.005,
    "redepositionRate": 0.001,
    "redepositionThreshold": 0.01,
    "redepositionTimeInt": 0.1,
    "diffusionCoefficient": 0.02,
    "sinkStrength": 0.1,
    "scallopVelocity": 0.05,
    "centerVelocity": 0.05,
    "topHeight": 0.6,
    "centerWidth": 1.2,
    "stabilityFactor": 0.15,
}


class OxideRegrowthParams(BaseModel):
    """Parameters of one native ``ps.OxideRegrowth`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by ``backend.validation`` is accepted here and vice versa.
    ``duration`` is the native ``ps.Process`` duration in seconds and must be
    strictly positive because the model has no other time control.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    nitrideEtchRate: float = Field(default=_DEFAULTS["nitrideEtchRate"], ge=0.0, le=_MAXIMUM)
    oxideEtchRate: float = Field(default=_DEFAULTS["oxideEtchRate"], ge=0.0, le=_MAXIMUM)
    redepositionRate: float = Field(default=_DEFAULTS["redepositionRate"], ge=0.0, le=_MAXIMUM)
    redepositionThreshold: float = Field(default=_DEFAULTS["redepositionThreshold"], ge=0.0, le=_MAXIMUM)
    redepositionTimeInt: float = Field(default=_DEFAULTS["redepositionTimeInt"], ge=0.0, le=_MAXIMUM)
    diffusionCoefficient: float = Field(default=_DEFAULTS["diffusionCoefficient"], ge=0.0, le=_MAXIMUM)
    sinkStrength: float = Field(default=_DEFAULTS["sinkStrength"], ge=0.0, le=_MAXIMUM)
    scallopVelocity: float = Field(default=_DEFAULTS["scallopVelocity"], ge=0.0, le=_MAXIMUM)
    centerVelocity: float = Field(default=_DEFAULTS["centerVelocity"], ge=0.0, le=_MAXIMUM)
    topHeight: float = Field(default=_DEFAULTS["topHeight"], ge=0.0, le=_MAXIMUM)
    centerWidth: float = Field(default=_DEFAULTS["centerWidth"], ge=0.0, le=_MAXIMUM)
    stabilityFactor: float = Field(default=_DEFAULTS["stabilityFactor"], ge=0.0, le=_MAXIMUM)

    @field_validator(*FLAT_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    def flat_arguments(self) -> tuple[float, ...]:
        """Return the twelve values in ``ps.OxideRegrowth`` argument order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def native_values(self) -> dict[str, float]:
        """Return the twelve values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}
