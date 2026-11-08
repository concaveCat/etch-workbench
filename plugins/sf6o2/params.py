"""Canonical parameters for the native SF6/O2 plasma-etch plugin.

The eight flat fields and their defaults/ranges mirror
``backend.specialized_models.PLASMA_FLAT_FIELDS`` for the ``sf6o2`` catalog
row. They are repeated here instead of imported so that importing the plugin
never imports :mod:`backend` (the plugin must load without the worker or the
API layer). The bounds are the same ones ``backend.validation`` enforces on
``nativeParameters``: ``0 <= value <= 1e6`` except ``etchStopDepth``, whose
documented floor is ``-1e6``.

``coverageTolerance`` is optional. ``None`` means "the step did not request a
tolerance": the worker has always pinned ``CoverageParameters.tolerance`` to
``WORKBENCH_COVERAGE_TOLERANCE`` in that case, and the native variant does the
same so both paths agree. An explicit value is used verbatim.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["FLAT_FIELDS", "SF6O2Params", "WORKBENCH_COVERAGE_TOLERANCE"]

#: Native constructor argument order of ``ps.SF6O2Etching``.
FLAT_FIELDS: tuple[str, ...] = (
    "ionFlux",
    "etchantFlux",
    "oxygenFlux",
    "meanIonEnergy",
    "sigmaIonEnergy",
    "ionExponent",
    "oxySputterYield",
    "etchStopDepth",
)

#: The worker's historical ``CoverageParameters.tolerance`` default.
#: The native struct default is ``0.0`` (10 fixed initialization iterations).
WORKBENCH_COVERAGE_TOLERANCE = 1e-3

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "ionFlux": 1.0,
    "etchantFlux": 1.0,
    "oxygenFlux": 0.2,
    "meanIonEnergy": 100.0,
    "sigmaIonEnergy": 10.0,
    "ionExponent": 100.0,
    "oxySputterYield": 3.0,
    "etchStopDepth": -1_000_000.0,
}

_MINIMUMS: dict[str, float] = {name: 0.0 for name in FLAT_FIELDS}
_MINIMUMS["etchStopDepth"] = -1_000_000.0


class SF6O2Params(BaseModel):
    """Parameters of one native ``ps.SF6O2Etching`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by ``backend.validation`` is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    ionFlux: float = Field(default=_DEFAULTS["ionFlux"], ge=_MINIMUMS["ionFlux"], le=_MAXIMUM)
    etchantFlux: float = Field(default=_DEFAULTS["etchantFlux"], ge=_MINIMUMS["etchantFlux"], le=_MAXIMUM)
    oxygenFlux: float = Field(default=_DEFAULTS["oxygenFlux"], ge=_MINIMUMS["oxygenFlux"], le=_MAXIMUM)
    meanIonEnergy: float = Field(default=_DEFAULTS["meanIonEnergy"], ge=_MINIMUMS["meanIonEnergy"], le=_MAXIMUM)
    sigmaIonEnergy: float = Field(default=_DEFAULTS["sigmaIonEnergy"], ge=_MINIMUMS["sigmaIonEnergy"], le=_MAXIMUM)
    ionExponent: float = Field(default=_DEFAULTS["ionExponent"], ge=_MINIMUMS["ionExponent"], le=_MAXIMUM)
    oxySputterYield: float = Field(default=_DEFAULTS["oxySputterYield"], ge=_MINIMUMS["oxySputterYield"], le=_MAXIMUM)
    etchStopDepth: float = Field(default=_DEFAULTS["etchStopDepth"], ge=_MINIMUMS["etchStopDepth"], le=_MAXIMUM)
    coverageTolerance: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator(*FLAT_FIELDS, "duration", "coverageTolerance")
    @classmethod
    def _finite(cls, value: float | None, info: Any) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    def flat_arguments(self) -> tuple[float, ...]:
        """Return the eight values in ``ps.SF6O2Etching`` argument order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def flat_values(self) -> dict[str, float]:
        """Return the eight values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}
