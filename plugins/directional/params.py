"""Canonical parameters for the native directional plugin.

The three modes mirror ``backend.models.DirectionalStepV5`` and the three
native ``ps.DirectionalProcess`` overloads the worker drives:

* ``scalar`` - one direction plus non-negative directional/isotropic rates;
* ``rateSets`` - one ``ps.RateSet`` per entry, 1..8 entries;
* ``materialRates`` - a per-material ``(directional, isotropic)`` table plus
  an optional default row.

Every rate here is a requested magnitude. The operation selects the native
sign: the scalar and rate-set overloads multiply both components by ``-1`` for
etch, while the material overload uses the directional/isotropic sign pair from
``worker/simulate.py``. The names are repeated instead of imported so importing
the plugin never imports :mod:`backend` or :mod:`worker`.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from process_models.v2.options import AdvectionOverride

__all__ = [
    "DirectionalMaterialRate",
    "DirectionalParams",
    "DirectionalRateSet",
    "DirectionalMode",
]

DirectionalMode = Literal["scalar", "rateSets", "materialRates"]

_MAXIMUM_RATE = 2.0
_MAXIMUM_RATE_SETS = 8
_MAXIMUM_MASK_MATERIALS = 16
_MAXIMUM_MATERIAL_NAME = 80


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _direction(value: list[float], name: str) -> list[float]:
    for index, component in enumerate(value):
        _finite(component, f"{name}[{index}]")
    length = math.sqrt(sum(component * component for component in value))
    if length <= 1e-12:
        raise ValueError(f"{name} must be a nonzero 3-component vector")
    return value


class DirectionalRateSet(BaseModel):
    """One real ``viennaps.RateSet`` of the native multi-set overload.

    ``maskMaterials=None`` means "derive the masks from the project geometry",
    exactly like the scalar directional path. An explicit list (including the
    empty list) is honoured verbatim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    direction: list[float] = Field(min_length=3, max_length=3)
    directionalVelocity: float = Field(ge=0.0, le=_MAXIMUM_RATE)
    isotropicVelocity: float = Field(default=0.0, ge=0.0, le=_MAXIMUM_RATE)
    maskMaterials: list[str] | None = Field(default=None, max_length=_MAXIMUM_MASK_MATERIALS)
    calculateVisibility: bool = True

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, value: list[float]) -> list[float]:
        return _direction(value, "direction")

    @field_validator("directionalVelocity", "isotropicVelocity")
    @classmethod
    def _valid_rate(cls, value: float, info: Any) -> float:
        return _finite(value, info.field_name)

    @field_validator("maskMaterials")
    @classmethod
    def _valid_mask_materials(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for name in value:
            if not name.strip() or len(name) > _MAXIMUM_MATERIAL_NAME:
                raise ValueError("maskMaterials entries must be non-empty names of at most 80 characters")
        if len(set(value)) != len(value):
            raise ValueError("maskMaterials must not contain duplicate material names")
        return value


class DirectionalMaterialRate(BaseModel):
    """One native ``RateMap`` entry: non-negative requested rates."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    directional: float = Field(default=0.0, ge=-_MAXIMUM_RATE, le=_MAXIMUM_RATE)
    isotropic: float = Field(default=0.0, ge=-_MAXIMUM_RATE, le=_MAXIMUM_RATE)

    @field_validator("directional", "isotropic")
    @classmethod
    def _valid_rate(cls, value: float, info: Any) -> float:
        return _finite(value, info.field_name)


class DirectionalParams(BaseModel):
    """Parameters of one native ``ps.DirectionalProcess`` step.

    The mode is selected by the fields that are present, not by a separate
    discriminator, so the canonical request retains the same shape as the
    project step. ``materialRates`` and ``defaultMaterialRate`` win over
    ``rateSets``; both are mutually exclusive with the scalar fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Literal["etch", "deposit"]
    duration: float = Field(gt=0.0)
    direction: list[float] | None = Field(default=None, min_length=3, max_length=3)
    directionalRateMagnitude: float | None = Field(default=None, ge=0.0, le=_MAXIMUM_RATE)
    isotropicRateMagnitude: float | None = Field(default=None, ge=0.0, le=_MAXIMUM_RATE)
    calculateVisibility: bool = True
    rateSets: list[DirectionalRateSet] | None = Field(default=None, min_length=1, max_length=_MAXIMUM_RATE_SETS)
    materialRates: dict[str, DirectionalMaterialRate] | None = None
    defaultMaterialRate: DirectionalMaterialRate | None = None
    rateSigns: Literal["operation", "native"] | None = None
    advection: AdvectionOverride | None = None
    depositMaterial: str | None = Field(default=None, min_length=1, max_length=_MAXIMUM_MATERIAL_NAME)

    @field_validator("duration")
    @classmethod
    def _valid_duration(cls, value: float) -> float:
        return _finite(value, "duration")

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        return _direction(value, "direction")

    @field_validator("directionalRateMagnitude", "isotropicRateMagnitude")
    @classmethod
    def _valid_rate(cls, value: float | None, info: Any) -> float | None:
        if value is None:
            return None
        return _finite(value, info.field_name)

    @field_validator("materialRates")
    @classmethod
    def _valid_material_rates(
        cls, value: dict[str, DirectionalMaterialRate] | None
    ) -> dict[str, DirectionalMaterialRate] | None:
        if value is None:
            return None
        for name in value:
            if not name.strip() or len(name) > _MAXIMUM_MATERIAL_NAME:
                raise ValueError("materialRates keys must be non-empty names of at most 80 characters")
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> "DirectionalParams":
        scalar_fields = ("direction", "directionalRateMagnitude", "isotropicRateMagnitude")
        material_mode = self.materialRates is not None or self.defaultMaterialRate is not None
        if self.rateSigns is not None and not material_mode:
            raise ValueError("rateSigns applies only to materialRates mode")
        if material_mode:
            if any(name in self.model_fields_set for name in scalar_fields[1:]):
                raise ValueError(
                    "materialRates is mutually exclusive with directionalRateMagnitude and "
                    "isotropicRateMagnitude: omit both scalar fields in material mode"
                )
            if self.rateSets is not None:
                raise ValueError("materialRates is mutually exclusive with rateSets")
            if self.direction is None:
                raise ValueError("material rate mode requires direction")
            if self.calculateVisibility is not True:
                raise ValueError(
                    "material rate mode has no native visibility control; omit calculateVisibility "
                    "instead of materializing it (or leave it true)"
                )
            table = self.materialRates or {}
            if self.rateSigns != "native":
                negative = sorted(name for name, row in table.items() if row.directional < 0.0 or row.isotropic < 0.0)
                if self.defaultMaterialRate is not None and (
                    self.defaultMaterialRate.directional < 0.0 or self.defaultMaterialRate.isotropic < 0.0
                ):
                    negative.append("defaultMaterialRate")
                if negative:
                    raise ValueError(
                        "material rates must be non-negative unless rateSigns='native': " + ", ".join(negative)
                    )
            default = self.defaultMaterialRate
            effective = any(row.directional != 0.0 or row.isotropic != 0.0 for row in table.values()) or (
                default is not None and (default.directional != 0.0 or default.isotropic != 0.0)
            )
            if not effective:
                raise ValueError("material rate mode requires at least one nonzero directional or isotropic rate")
        elif self.rateSets is None:
            if any(getattr(self, name) is None for name in scalar_fields):
                raise ValueError(
                    "directional steps require direction, directionalRateMagnitude and "
                    "isotropicRateMagnitude unless rateSets is used"
                )
        else:
            if any(name in self.model_fields_set for name in scalar_fields):
                raise ValueError(
                    "rateSets is mutually exclusive with direction, directionalRateMagnitude "
                    "and isotropicRateMagnitude: omit all three fields in rateSets mode"
                )
            if self.calculateVisibility is not True:
                raise ValueError(
                    "rateSets uses per-set calculateVisibility; omit the step-level flag "
                    "instead of materializing it (or leave it true)"
                )
        if self.operation == "deposit" and self.depositMaterial is None:
            raise ValueError("deposit steps require depositMaterial")
        if self.operation == "etch" and "depositMaterial" in self.model_fields_set:
            raise ValueError("depositMaterial applies only to deposit steps")
        return self

    def mode(self) -> DirectionalMode:
        """Return the native overload selected by the present fields."""
        if self.materialRates is not None or self.defaultMaterialRate is not None:
            return "materialRates"
        if self.rateSets is not None:
            return "rateSets"
        return "scalar"

    def unit_direction(self) -> list[float]:
        """Return the normalized step direction (material and scalar modes)."""
        if self.direction is None:
            raise ValueError("directional step has no direction")
        length = math.sqrt(sum(component * component for component in self.direction))
        return [component / length for component in self.direction]
