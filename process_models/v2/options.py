"""Step-level native solver overrides shared by v2 execution and the schema.

The project-level ``solver.advection`` block stays the default for every step.
The FinFET reference script changes only two advection schemes for individual
processes and one static dissipation limit, so a step may carry an optional
override instead of forcing a whole-project setting.

Every field is optional and omitted fields keep the project default. The
object is serialized with ``exclude_none`` by the canonical request hash, so
adding it never changes an existing project hash.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["AdvectionOverride", "SPATIAL_SCHEMES", "TEMPORAL_SCHEMES"]

SPATIAL_SCHEMES = (
    "ENGQUIST_OSHER_1ST_ORDER",
    "ENGQUIST_OSHER_2ND_ORDER",
    "LAX_FRIEDRICHS_1ST_ORDER",
    "LAX_FRIEDRICHS_2ND_ORDER",
    "LOCAL_LAX_FRIEDRICHS_ANALYTICAL_1ST_ORDER",
    "LOCAL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER",
    "LOCAL_LOCAL_LAX_FRIEDRICHS_2ND_ORDER",
    "LOCAL_LAX_FRIEDRICHS_1ST_ORDER",
    "LOCAL_LAX_FRIEDRICHS_2ND_ORDER",
    "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER",
    "WENO_3RD_ORDER",
    "WENO_5TH_ORDER",
)
TEMPORAL_SCHEMES = ("FORWARD_EULER", "RUNGE_KUTTA_2ND_ORDER", "RUNGE_KUTTA_3RD_ORDER")

_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class AdvectionOverride(BaseModel):
    """One step's native ``AdvectionParameters`` overrides.

    ``maxDissipation`` is the static
    ``StencilLocalLaxFriedrichsScalar.setMaxDissipation`` value. It is only
    meaningful when the effective spatial scheme is
    ``STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER``; the variant checks the effective
    scheme (step override or project default) before applying it.
    """

    model_config = _CONFIG

    spatialScheme: Literal[SPATIAL_SCHEMES] | None = None
    temporalScheme: Literal[TEMPORAL_SCHEMES] | None = None
    timeStepRatio: float | None = Field(default=None, ge=0.05, le=0.5)
    maxDissipation: float | None = Field(default=None, ge=0.0, le=1_000_000.0)

    @model_validator(mode="after")
    def check_non_empty(self) -> "AdvectionOverride":
        if not any(getattr(self, name) is not None for name in type(self).model_fields):
            raise ValueError("advection override must set at least one field")
        return self
