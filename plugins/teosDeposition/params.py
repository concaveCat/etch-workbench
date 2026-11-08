"""Canonical parameters for the native TEOS thermal-deposition plugin.

The six flat fields and their defaults/ranges mirror
``backend.specialized_models`` for the ``teosDeposition`` catalog row. They are
repeated here instead of imported so that importing the plugin never imports
:mod:`backend` (the plugin must load without the worker or the API layer).
Sticking probabilities are in ``[0, 1]``; rates and reaction orders are
non-negative. Every value must be finite.

Unlike the plasma-etch plugins there is no ``coverageTolerance``: the native
``TEOSDeposition`` model has no coverage-tolerance parameter, so this plugin
must not invent one.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["FLAT_FIELDS", "TEOSDepositionParams"]

#: Native constructor argument order of ``ps.TEOSDeposition``.
FLAT_FIELDS: tuple[str, ...] = (
    "stickingProbabilityP1",
    "rateP1",
    "orderP1",
    "stickingProbabilityP2",
    "rateP2",
    "orderP2",
)

_MAXIMUM = 1_000_000.0

_DEFAULTS: dict[str, float] = {
    "stickingProbabilityP1": 0.5,
    "rateP1": 0.05,
    "orderP1": 1.0,
    "stickingProbabilityP2": 0.0,
    "rateP2": 0.0,
    "orderP2": 0.0,
}


class TEOSDepositionParams(BaseModel):
    """Parameters of one native ``ps.TEOSDeposition`` step.

    Every numeric field must be finite; the ranges are the catalog's own so a
    step accepted by the project validator is accepted here and vice versa.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0)
    stickingProbabilityP1: float = Field(default=_DEFAULTS["stickingProbabilityP1"], ge=0.0, le=1.0)
    rateP1: float = Field(default=_DEFAULTS["rateP1"], ge=0.0, le=_MAXIMUM)
    orderP1: float = Field(default=_DEFAULTS["orderP1"], ge=0.0, le=_MAXIMUM)
    stickingProbabilityP2: float = Field(default=_DEFAULTS["stickingProbabilityP2"], ge=0.0, le=1.0)
    rateP2: float = Field(default=_DEFAULTS["rateP2"], ge=0.0, le=_MAXIMUM)
    orderP2: float = Field(default=_DEFAULTS["orderP2"], ge=0.0, le=_MAXIMUM)

    @field_validator(*FLAT_FIELDS, "duration")
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    def flat_arguments(self) -> tuple[float, ...]:
        """Return the six values in ``ps.TEOSDeposition`` argument order."""
        return tuple(float(getattr(self, name)) for name in FLAT_FIELDS)

    def flat_values(self) -> dict[str, float]:
        """Return the six values keyed by their native parameter name."""
        return {name: float(getattr(self, name)) for name in FLAT_FIELDS}
