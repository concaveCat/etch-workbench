"""Canonical parameters for the native implant field plugin.

These models mirror the schema-7 ``ImplantStepV7`` subset that the worker has
verified on ViennaPS 4.7.0.  They are repeated here instead of importing
:mod:`backend` so the plugin can load without the API layer or the worker.
Table profiles are deliberately absent: the public wheel ships no model
database.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "CellSetParams",
    "ImplantDamageParams",
    "ImplantLabelsParams",
    "ImplantParams",
    "ManualImplantProfile",
    "PearsonIVParameters",
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


class PearsonIVParameters(BaseModel):
    """One native ``PearsonIVParameters`` moment set (project length unit)."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    mu: float = Field(ge=0, le=10000)
    sigma: float = Field(gt=0, le=10000)
    beta: float = Field(gt=0, le=1000)
    gamma: float = Field(ge=-1000, le=1000)


class ManualImplantProfile(BaseModel):
    """An explicit Pearson IV, dual Pearson IV, or channeling profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    source: Literal["manual"]
    kind: Literal["pearsonIV", "dualPearsonIV", "pearsonIVChanneling"]
    head: PearsonIVParameters
    lateralMu: float = 0
    lateralSigma: float = Field(default=5, gt=0, le=10000)
    tail: PearsonIVParameters | None = None
    headFraction: float | None = Field(default=None, ge=0, le=1)
    tailLateralMu: float | None = None
    tailLateralSigma: float | None = Field(default=None, gt=0, le=10000)
    tailFraction: float | None = Field(default=None, ge=0, le=1)
    tailStartDepth: float | None = Field(default=None, ge=0, le=10000)
    tailDecayLength: float | None = Field(default=None, gt=0, le=10000)
    tailBlendWidth: float = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def check_kind_fields(self) -> "ManualImplantProfile":
        dual = (self.tail, self.headFraction, self.tailLateralMu, self.tailLateralSigma)
        channeling = (self.tailFraction, self.tailStartDepth, self.tailDecayLength)
        if self.kind == "pearsonIV":
            if any(value is not None for value in dual + channeling):
                raise ValueError("pearsonIV does not accept tail/channeling parameters")
            if float(self.tailBlendWidth) != 0.0:
                raise ValueError("tailBlendWidth applies only to pearsonIVChanneling")
        elif self.kind == "dualPearsonIV":
            missing = [
                name
                for name, value in zip(("tail", "headFraction", "tailLateralMu", "tailLateralSigma"), dual)
                if value is None
            ]
            if missing:
                raise ValueError("dualPearsonIV requires " + ", ".join(missing))
            if any(value is not None for value in channeling) or float(self.tailBlendWidth) != 0.0:
                raise ValueError("dualPearsonIV does not accept channeling parameters")
        else:
            missing = [
                name
                for name, value in zip(("tailFraction", "tailStartDepth", "tailDecayLength"), channeling)
                if value is None
            ]
            if missing:
                raise ValueError("pearsonIVChanneling requires " + ", ".join(missing))
            if any(value is not None for value in dual):
                raise ValueError("pearsonIVChanneling does not accept dual-Pearson parameters")
        return self


class ImplantDamageParams(BaseModel):
    """The installed ``ImplantDamageHobler`` constructor arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["hobler"]
    projectedRange: float = Field(gt=0, le=10000)
    verticalSigma: float = Field(gt=0, le=10000)
    lambdaValue: float = Field(gt=0, le=100000)
    defectsPerIon: float = Field(ge=0, le=100000)
    lateralSigma: float = Field(gt=0, le=10000)
    lateralDeltaSigma: float = Field(default=0, ge=-10000, le=10000)


class ImplantLabelsParams(BaseModel):
    """Optional explicit native cell-set labels."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    concentration: str | None = None
    damage: str | None = None
    lastDamage: str | None = None

    @model_validator(mode="after")
    def check_labels(self) -> "ImplantLabelsParams":
        for name in ("concentration", "damage", "lastDamage"):
            value = getattr(self, name)
            if value is None:
                continue
            if _LABEL.fullmatch(value) is None:
                raise ValueError(f"{name} must be a valid cell-set label")
            if value in _RESERVED:
                raise ValueError(f"{name} {value!r} is reserved by the native cell set")
        return self


class ImplantParams(BaseModel):
    """Parameters of one native ``IonImplantation`` field step."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    species: str = Field(min_length=1, max_length=24)
    material: str = Field(min_length=1, max_length=64)
    energyKeV: float = Field(gt=0, le=100000)
    doseCm2: float = Field(gt=0, le=1e19)
    tiltDeg: float = Field(ge=0, le=90)
    rotationDeg: float = Field(default=0, ge=0, le=360)
    doseControl: Literal["Off", "WaferDose", "BeamDose"] = "WaferDose"
    profile: ManualImplantProfile
    damage: ImplantDamageParams | None = None
    maskMaterials: list[str] = Field(default_factory=list)
    screenMaterials: list[str] = Field(default_factory=list)
    labels: ImplantLabelsParams | None = None
    damageFactor: float = Field(default=1, gt=0, le=1000000)
    cellSet: CellSetParams

    @model_validator(mode="after")
    def check_implant_fields(self) -> "ImplantParams":
        if float(self.rotationDeg) != 0.0:
            raise ValueError(
                "rotationDeg is only consumed by ImplantTableModel; the manual Pearson IV "
                "path has no rotation parameter, so only rotationDeg=0 is accepted"
            )
        for name in ("maskMaterials", "screenMaterials"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicates")
        if set(self.maskMaterials) & set(self.screenMaterials):
            raise ValueError("maskMaterials and screenMaterials must be disjoint")
        if self.material in set(self.maskMaterials) | set(self.screenMaterials):
            raise ValueError("material must not also be a mask or screen material")

        resolved = self.labels or ImplantLabelsParams()
        stem = re.sub(r"[^A-Za-z0-9_]", "_", self.species)
        if not re.match(r"^[A-Za-z]", stem):
            stem = "dopant_" + stem
        concentration = resolved.concentration or f"{stem}_total"
        damage = resolved.damage
        last_damage = resolved.lastDamage
        if self.damage is None:
            if damage is not None or last_damage is not None:
                raise ValueError("labels.damage/lastDamage require a damage model")
        else:
            damage = damage or f"{stem}_damage"
            last_damage = last_damage or f"{damage}_last"
        if len({concentration, damage, last_damage} - {None}) != len(
            [value for value in (concentration, damage, last_damage) if value is not None]
        ):
            raise ValueError("implant labels must be distinct")
        object.__setattr__(
            self,
            "labels",
            ImplantLabelsParams(
                concentration=concentration,
                damage=damage,
                lastDamage=last_damage,
            ),
        )
        return self

    @property
    def profile_kind(self) -> str:
        return self.profile.kind
