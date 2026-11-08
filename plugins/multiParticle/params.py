"""Canonical parameters for the native multi-particle plasma plugin.

The fields mirror ``backend.models.MultiParticleStepV7``,
``IonParticleV7``/``NeutralParticleV7`` and
``MultiParticleRateCoefficientsV7``: the operation, a positive duration, the
optional deposit material, one to eight species, and the fixed weighted-sum
rate table. They are repeated here instead of imported so that importing the
plugin never imports :mod:`backend` or :mod:`worker`.

The rate table is intentionally data, not code: a native velocity is
``operationSign * sum(flux[i] * coefficient[material][species[i]])``. The
plugin never accepts a user callback.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "IonParticle",
    "MultiParticleParams",
    "MultiParticleRateCoefficients",
    "NeutralParticle",
    "Operation",
    "ParticleLabel",
    "RateCoefficient",
]

Operation = Literal["etch", "deposit"]
ParticleLabel = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
RateCoefficient = Annotated[float, Field(ge=0.0, le=2.0)]

_MAX_SPECIES = 8


class IonParticle(BaseModel):
    """One ion species, mirroring ``backend.models.IonParticleV7``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["ion"]
    label: ParticleLabel
    sourcePower: float = Field(ge=0.0, le=20.0)
    thetaRMin: float = Field(default=0.0, ge=0.0, le=90.0)
    thetaRMax: float = Field(default=90.0, ge=0.0, le=90.0)
    minAngle: float = Field(default=80.0, ge=0.0, le=90.0)
    B_sp: float = Field(default=-1.0, ge=-1.0, le=20.0)
    meanEnergy: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    sigmaEnergy: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    thresholdEnergy: float = Field(default=0.0, ge=0.0, le=1_000_000.0)
    inflectAngle: float = Field(default=0.0, ge=0.0, le=90.0)
    n: int = Field(default=1, ge=1, le=100)

    @field_validator(
        "sourcePower",
        "thetaRMin",
        "thetaRMax",
        "minAngle",
        "B_sp",
        "meanEnergy",
        "sigmaEnergy",
        "thresholdEnergy",
        "inflectAngle",
    )
    @classmethod
    def _finite(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @model_validator(mode="after")
    def _angles(self) -> IonParticle:
        if self.thetaRMin > self.thetaRMax:
            raise ValueError("thetaRMin must not exceed thetaRMax")
        return self


class NeutralParticle(BaseModel):
    """One neutral species, mirroring ``backend.models.NeutralParticleV7``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["neutral"]
    label: ParticleLabel
    defaultStickingProbability: Probability = 1.0
    stickingProbabilityByMaterial: dict[str, Probability] = Field(default_factory=dict)

    @field_validator("defaultStickingProbability")
    @classmethod
    def _finite_default(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("stickingProbabilityByMaterial")
    @classmethod
    def _sticking_by_material(cls, value: dict[str, float]) -> dict[str, float]:
        for name, probability in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("stickingProbabilityByMaterial keys must be non-empty material names")
            if not math.isfinite(probability):
                raise ValueError(f"stickingProbabilityByMaterial[{name!r}] must be finite")
        return value


ParticleSpecies = Annotated[Union[IonParticle, NeutralParticle], Field(discriminator="kind")]


class MultiParticleRateCoefficients(BaseModel):
    """Fixed weighted-sum rate table, mirroring the V7 coefficient model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    defaultBySpecies: dict[ParticleLabel, RateCoefficient]
    byMaterial: dict[str, dict[ParticleLabel, RateCoefficient]] = Field(default_factory=dict)

    @field_validator("byMaterial")
    @classmethod
    def _materials(cls, value: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for name, row in value.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("byMaterial keys must be non-empty material names")
            if not isinstance(row, dict):
                raise ValueError(f"byMaterial[{name!r}] must be a species coefficient object")
        return value


class MultiParticleParams(BaseModel):
    """Parameters of one native ``ps.MultiParticleProcess`` step."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Operation
    duration: float = Field(gt=0.0)
    depositMaterial: str | None = None
    species: Annotated[list[ParticleSpecies], Field(min_length=1, max_length=_MAX_SPECIES)]
    rateCoefficients: MultiParticleRateCoefficients

    @field_validator("duration")
    @classmethod
    def _finite_duration(cls, value: float, info: Any) -> float:
        if not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("depositMaterial")
    @classmethod
    def _material_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("depositMaterial must be a non-empty material name")
        return value

    @model_validator(mode="after")
    def _step_rules(self) -> MultiParticleParams:
        if self.operation == "deposit":
            if self.depositMaterial is None:
                raise ValueError("multi-particle deposit steps require depositMaterial")
        elif self.depositMaterial is not None:
            raise ValueError("depositMaterial applies only to deposit steps")

        labels = [species.label for species in self.species]
        if len(labels) != len(set(labels)):
            raise ValueError("multi-particle species labels must be unique")
        label_set = set(labels)
        if set(self.rateCoefficients.defaultBySpecies) != label_set:
            raise ValueError("defaultBySpecies must contain exactly one coefficient per species label")
        for material, coefficients in self.rateCoefficients.byMaterial.items():
            unknown = set(coefficients) - label_set
            if unknown:
                raise ValueError(
                    f"material rate coefficients for {material!r} contain unknown species labels {sorted(unknown)!r}"
                )
        return self

    def labels(self) -> tuple[str, ...]:
        """Return species labels in native registration order."""
        return tuple(str(species.label) for species in self.species)

    def default_coefficients(self) -> dict[str, float]:
        """Return the per-species default coefficients in species order."""
        labels = self.labels()
        return {label: float(self.rateCoefficients.defaultBySpecies[label]) for label in labels}

    def material_coefficients(self) -> dict[str, dict[str, float]]:
        """Return the per-material coefficient overrides as plain floats."""
        return {
            str(material): {str(label): float(value) for label, value in row.items()}
            for material, row in self.rateCoefficients.byMaterial.items()
        }
