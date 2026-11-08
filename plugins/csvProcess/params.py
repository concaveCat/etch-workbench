"""Canonical parameters for the native CSV rate-grid process."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["CsvProcessParams"]

_HEX = frozenset("0123456789abcdef")


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


class CsvProcessParams(BaseModel):
    """Parameters of one native ``ps.CSVFileProcess`` step."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    duration: float = Field(gt=0.0)
    assetId: str = Field(min_length=64, max_length=64)
    fileName: str = Field(min_length=1, max_length=255)
    direction: list[float] = Field(min_length=3, max_length=3)
    offset: list[float] = Field(min_length=2, max_length=2)
    isotropicComponent: float = Field(default=0.0, ge=0.0, le=100.0)
    directionalComponent: float = Field(default=1.0, ge=0.0, le=100.0)
    interpolationMode: Literal["LINEAR", "IDW"]
    idwNeighbors: int | None = Field(default=None, ge=1, le=64)
    calculateVisibility: bool = True

    @field_validator("duration", "isotropicComponent", "directionalComponent")
    @classmethod
    def _finite_value(cls, value: float, info) -> float:
        return _finite(value, info.field_name)

    @field_validator("assetId")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in _HEX for character in value):
            raise ValueError("assetId must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("fileName")
    @classmethod
    def _file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("fileName must be a plain file name")
        return value

    @field_validator("direction")
    @classmethod
    def _direction(cls, value: list[float]) -> list[float]:
        values = [float(component) for component in value]
        for index, component in enumerate(values):
            _finite(component, f"direction[{index}]")
        if sum(component * component for component in values) <= 1e-24:
            raise ValueError("direction must be a finite nonzero vector")
        return values

    @field_validator("offset")
    @classmethod
    def _offset(cls, value: list[float]) -> list[float]:
        values = [float(component) for component in value]
        for index, component in enumerate(values):
            _finite(component, f"offset[{index}]")
        return values

    @model_validator(mode="after")
    def _interpolation(self) -> "CsvProcessParams":
        if self.interpolationMode == "IDW" and self.idwNeighbors is None:
            raise ValueError("IDW interpolation requires idwNeighbors")
        if self.interpolationMode == "LINEAR" and self.idwNeighbors is not None:
            raise ValueError("idwNeighbors applies only to IDW interpolation")
        if self.isotropicComponent == 0.0 and self.directionalComponent == 0.0:
            raise ValueError("at least one of isotropicComponent or directionalComponent must be nonzero")
        return self

    def unit_direction(self) -> list[float]:
        length = math.sqrt(sum(component * component for component in self.direction))
        return [component / length for component in self.direction]
