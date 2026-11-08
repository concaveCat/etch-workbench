"""Canonical parameters for the native anneal field plugin.

Only the branches verified on ViennaPS 4.7.0 are exposed: isothermal or
schedule temperature, Arrhenius or constant diffusivity, the two native solver
modes, optional implicit-solver controls, diffusion/blocking materials, and
solid activation. Defect coupling, TED, clustering, and source injection are
not part of this slice.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AnnealDiffusivity",
    "AnnealParams",
    "AnnealSchedule",
    "CellSetParams",
    "ImplicitSolverOptions",
    "SolidActivation",
]

_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}")
_RESERVED = frozenset({"Material", "FillingFraction"})


class CellSetParams(BaseModel):
    """The native cell-set lattice settings carried by a field step."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    height: float = Field(gt=0, le=1000)
    substrateDepth: float | None = Field(default=None, gt=0, le=1000)
    coverMaterial: str = Field(default="GAS", min_length=1, max_length=64)
    periodic: bool = False
    format: Literal["vtu", "json", "both"] = "both"
    scalarLabels: list[str] | None = None

    @model_validator(mode="after")
    def check_settings(self) -> "CellSetParams":
        if self.coverMaterial != "GAS":
            raise ValueError(
                "cellSet.coverMaterial must be GAS for implant/anneal/analysis: the native "
                "DenseCellSet generator treats the cover material as the phase above the "
                "surface and would void a target with the same material"
            )
        if self.scalarLabels is not None:
            if not self.scalarLabels:
                raise ValueError("cellSet.scalarLabels must be omitted or a non-empty list")
            if len(set(self.scalarLabels)) != len(self.scalarLabels):
                raise ValueError("cellSet.scalarLabels must not contain duplicates")
            for label in self.scalarLabels:
                if _LABEL.fullmatch(label) is None:
                    raise ValueError(f"cellSet.scalarLabels entry {label!r} is not a valid cell-set label")
                if label in _RESERVED:
                    raise ValueError(f"cellSet.scalarLabels entry {label!r} is reserved by the native cell set")
        return self


class IsothermalSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["isothermal"]
    temperatureK: float = Field(gt=0, le=5000)
    durationSeconds: float = Field(ge=0, le=1000000)


class StepSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["schedule"]
    durations: list[float] = Field(min_length=1, max_length=32)
    temperatures: list[float] = Field(min_length=1, max_length=33)

    @model_validator(mode="after")
    def check_lengths(self) -> "StepSchedule":
        if len(self.temperatures) not in (len(self.durations), len(self.durations) + 1):
            raise ValueError(
                "schedule.temperatures must contain N isothermal values or N+1 ramp endpoints "
                f"for {len(self.durations)} duration(s)"
            )
        if any(value < 0 for value in self.durations):
            raise ValueError("schedule.durations must be non-negative")
        if any(not value > 0 for value in self.temperatures):
            raise ValueError("schedule.temperatures must be positive")
        return self


AnnealSchedule = Annotated[Union[IsothermalSchedule, StepSchedule], Field(discriminator="kind")]


class ArrheniusDiffusivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["arrhenius"]
    D0: float = Field(gt=0, le=1e40)
    Ea_eV: float = Field(ge=0, le=100)


class ConstantDiffusivity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["constant"]
    value: float = Field(ge=0, le=1e40)


AnnealDiffusivity = Annotated[Union[ArrheniusDiffusivity, ConstantDiffusivity], Field(discriminator="kind")]


class ImplicitSolverOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    maxIterations: int = Field(default=400, ge=1, le=100000)
    relativeTolerance: float = Field(default=1e-6, gt=0, le=1)
    relaxation: float = Field(default=1.0, gt=0, le=2)


class SolidActivation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    solubilityC0: float = Field(gt=0, le=1e40)
    solubilityEa_eV: float = Field(ge=0, le=100)


class AnnealParams(BaseModel):
    """Parameters of one native ``Anneal`` field step."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    mode: Literal["GaussSeidel", "Explicit"] = "GaussSeidel"
    schedule: AnnealSchedule
    diffusivity: AnnealDiffusivity
    timeStep: float | None = Field(default=None, gt=0, le=1000000)
    stabilityFactor: float | None = Field(default=None, ge=0.01, le=1)
    implicitSolver: ImplicitSolverOptions | None = None
    diffusionMaterials: list[str] = Field(default_factory=list)
    blockingMaterials: list[str] = Field(default_factory=list)
    speciesLabel: str = Field(min_length=1, max_length=48)
    activeLabel: str = Field(min_length=1, max_length=48)
    solidActivation: SolidActivation
    activationOnly: bool = False
    cellSet: CellSetParams

    @model_validator(mode="after")
    def check_anneal_fields(self) -> "AnnealParams":
        for name in ("speciesLabel", "activeLabel"):
            value = getattr(self, name)
            if _LABEL.fullmatch(value) is None:
                raise ValueError(f"{name} must be a valid cell-set label")
            if value in _RESERVED:
                raise ValueError(f"{name} {value!r} is reserved by the native cell set")
        if self.speciesLabel == self.activeLabel:
            raise ValueError("speciesLabel and activeLabel must be different cell-set fields")
        if set(self.diffusionMaterials) & set(self.blockingMaterials):
            raise ValueError("diffusionMaterials and blockingMaterials must be disjoint")
        if len(set(self.diffusionMaterials)) != len(self.diffusionMaterials):
            raise ValueError("diffusionMaterials must not contain duplicates")
        if len(set(self.blockingMaterials)) != len(self.blockingMaterials):
            raise ValueError("blockingMaterials must not contain duplicates")
        if self.mode == "Explicit" and self.implicitSolver is not None:
            raise ValueError("implicitSolver applies only to mode=GaussSeidel")
        return self
