"""Canonical parameters for the native single-particle plasma plugin.

The fields mirror the applicable ``StepV4`` members for
``model="singleParticle"``: the operation, a non-negative ``rateMagnitude``,
the step duration, the two particle coefficients, and the optional material
rate table. They are repeated here instead of imported so that importing the
plugin never imports :mod:`backend` or :mod:`worker`.

The mode rules are the same ones ``backend.models.StepV4`` enforces:

* ``deposit`` requires a non-empty ``depositMaterial`` and ``etch`` forbids it;
* ``rateMode="uniform"`` forbids ``materialRates``;
* ``rateMode="material"`` requires ``materialRates`` (an empty table is valid
  and means "no material moves", which the worker's zero default rate
  reproduces).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["MaterialRate", "Operation", "RateMode", "SingleParticleParams"]

Operation = Literal["etch", "deposit"]
RateMode = Literal["uniform", "material"]

#: One non-negative material rate; the StepV4 bound is ``[0, 2]``.
MaterialRate = Annotated[float, Field(ge=0.0, le=2.0)]

_MAXIMUM_RATE = 2.0


class SingleParticleParams(BaseModel):
    """Parameters of one native ``ps.SingleParticleProcess`` step."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Operation
    rateMagnitude: float = Field(ge=0.0, le=_MAXIMUM_RATE)
    duration: float = Field(gt=0.0)
    stickingProbability: float = Field(ge=0.0, le=1.0)
    sourceExponent: float = Field(ge=0.0, le=20.0)
    depositMaterial: str | None = None
    rateMode: RateMode = "uniform"
    materialRates: dict[str, MaterialRate] | None = None

    @field_validator(
        "rateMagnitude",
        "duration",
        "stickingProbability",
        "sourceExponent",
    )
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("depositMaterial")
    @classmethod
    def _material_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("depositMaterial must be a non-empty material name")
        return value

    @field_validator("materialRates")
    @classmethod
    def _material_rates(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        for name, rate in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("materialRates keys must be non-empty material names")
            if not math.isfinite(rate):
                raise ValueError(f"materialRates[{name!r}] must be finite")
        return value

    @model_validator(mode="after")
    def _mode_rules(self) -> SingleParticleParams:
        if self.operation == "deposit":
            if self.depositMaterial is None:
                raise ValueError("deposit steps require depositMaterial")
        elif self.depositMaterial is not None:
            raise ValueError("depositMaterial applies only to deposit steps")

        if self.rateMode == "uniform":
            if self.materialRates is not None:
                raise ValueError("uniform rateMode forbids materialRates")
        elif self.materialRates is None:
            raise ValueError("material rateMode requires materialRates, which may be empty")
        return self

    def signed_material_rates(self) -> dict[str, float]:
        """Return material rates signed for the operation, as the worker does."""
        sign = -1.0 if self.operation == "etch" else 1.0
        return {name: float(rate) * sign for name, rate in (self.materialRates or {}).items()}
