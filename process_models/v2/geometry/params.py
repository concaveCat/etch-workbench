"""Canonical parameters for geometry operations.

Geometry operations are *not* physical process models: they have no duration,
flux engine, rate, or cell set. Each operation has its own parameter model so a
``cutoffHeight`` cannot be confused with a ``radius`` or a boolean position.
The shared :class:`process_models.v2.geometry.engine.GeometryEngine` consumes
the discriminated operation together with these models.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "BooleanParams",
    "GdsBooleanMaskParams",
    "GdsBooleanParams",
    "InsertBoxParams",
    "ParticleShapeParams",
    "PlanarizeParams",
    "RemoveMaterialParams",
    "RemoveTopLevelSetParams",
]

_CONFIG = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class InsertBoxParams(BaseModel):
    """One native ``LevelSet`` + ``MakeGeometry(Box)`` insertion.

    ``min``/``max`` are absolute project coordinates. The new level set is
    inserted as ``material`` above the current top level set.
    """

    model_config = _CONFIG

    min: Annotated[list[float], Field(min_length=3, max_length=3)]
    max: Annotated[list[float], Field(min_length=3, max_length=3)]
    material: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_box(self) -> "InsertBoxParams":
        if any(high <= low for low, high in zip(self.min, self.max)):
            raise ValueError("insertBox requires min < max on every axis")
        return self


class RemoveTopLevelSetParams(BaseModel):
    """One ``domain.removeTopLevelSet()`` call with no parameters."""

    model_config = _CONFIG


class PlanarizeParams(BaseModel):
    """One ``viennaps.Planarize(domain, cutoffHeight)`` call.

    The cutoff comes either from an explicit height or from the previous
    step's exposed top of ``stopMaterial`` (area-weighted p90, falling back to
    the median for older records) minus ``overPolish``. Both sources are kept
    exclusive so a stale height cannot silently override the measured stop.
    """

    model_config = _CONFIG

    cutoffHeight: float | None = None
    stopMaterial: str | None = Field(default=None, min_length=1, max_length=80)
    overPolish: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def check_cutoff_source(self) -> "PlanarizeParams":
        if (self.cutoffHeight is None) == (self.stopMaterial is None):
            raise ValueError("planarize requires exactly one of cutoffHeight or stopMaterial")
        if self.overPolish is not None and self.stopMaterial is None:
            raise ValueError("overPolish applies only to stopMaterial planarize")
        return self


class ParticleShapeParams(BaseModel):
    """One native ``BoxDistribution`` / ``SphereDistribution`` displacement.

    ``depositMaterial`` reproduces ``duplicateTopLevelSet`` before a positive
    geometric offset. ``maskMaterials`` overrides the project's geometry mask
    derivation; ``maskMode="allExcept"`` expands the domain's current materials
    minus that list, matching the native FinFET spacer etch.
    """

    model_config = _CONFIG

    operation: Literal["etch", "deposit"]
    shape: Literal["box", "sphere"]
    radius: float | None = Field(default=None, gt=0, le=2000)
    halfAxes: Annotated[list[float], Field(min_length=2, max_length=3)] | None = None
    depositMaterial: str | None = Field(default=None, min_length=1, max_length=80)
    maskMaterials: Annotated[list[str], Field(max_length=64)] | None = None
    maskMode: Literal["explicit", "allExcept"] | None = None

    @model_validator(mode="after")
    def check_shape_parameters(self) -> "ParticleShapeParams":
        if self.shape == "sphere":
            if self.radius is None:
                raise ValueError("sphere particleShape requires radius")
            if self.halfAxes is not None:
                raise ValueError("halfAxes applies only to shape=box")
        else:
            if self.halfAxes is None:
                raise ValueError("box particleShape requires halfAxes")
            if self.radius is not None:
                raise ValueError("radius applies only to shape=sphere")
        if self.operation == "etch" and self.depositMaterial is not None:
            raise ValueError("depositMaterial applies only to deposit particleShape steps")
        if self.maskMaterials is not None:
            if any(not name.strip() for name in self.maskMaterials):
                raise ValueError("maskMaterials entries must be non-empty material names")
            if len(set(self.maskMaterials)) != len(self.maskMaterials):
                raise ValueError("maskMaterials must not contain duplicate material names")
        if self.maskMode is not None and self.maskMaterials is None:
            raise ValueError("maskMode requires an explicit maskMaterials list")
        if self.operation == "deposit" and (self.maskMaterials is not None or self.maskMode is not None):
            raise ValueError(
                "masked particleShape deposition is not supported: ViennaPS 4.6.2/4.7.0 "
                "SIGSEGV on a non-empty mask with positive geometric advection"
            )
        return self


class BooleanParams(BaseModel):
    """One native boolean with a box or cylinder stencil operand.

    ``position`` is ``[x, h, 0]`` in 2D and ``[x, y, z]`` in 3D, matching the
    project schema. ``shape="box"`` uses width/height/depth; ``shape="cylinder"``
    uses ``radius`` for a vertical cylinder (3D only).
    """

    model_config = _CONFIG

    booleanOperation: Literal["union", "intersection", "difference"]
    position: Annotated[list[float], Field(min_length=3, max_length=3)]
    width: float = Field(gt=0, le=40)
    height: float = Field(gt=0, le=100)
    depth: float | None = Field(default=None, gt=0, le=40)
    angle: float = Field(default=0, ge=-45, le=45)
    applyToAll: bool = True
    shape: Literal["box", "cylinder"] = "box"
    radius: float | None = Field(default=None, gt=0, le=40)

    @model_validator(mode="after")
    def check_shape_parameters(self) -> "BooleanParams":
        if self.shape == "cylinder":
            if self.radius is None:
                raise ValueError("cylinder boolean requires radius")
        elif self.radius is not None:
            raise ValueError("radius applies only to shape=cylinder")
        return self


class GdsBooleanMaskParams(BaseModel):
    """One selected GDS layer/datatype extruded as a 3D boolean operand."""

    model_config = _CONFIG

    layer: int = Field(ge=0, le=65535)
    datatype: int = Field(default=0, ge=0, le=32767)
    unitScale: float = Field(default=1, gt=0, le=1000000)
    position: Annotated[list[float], Field(min_length=3, max_length=3)] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    baseHeight: float = Field(ge=0)
    height: float = Field(gt=0)
    retainedRegion: Literal["inside", "outside"] = "inside"


class GdsBooleanParams(BaseModel):
    """A GDS-asset boolean; the operand provider comes from the run assets."""

    model_config = _CONFIG

    booleanOperation: Literal["union", "intersection", "difference"]
    gdsMask: GdsBooleanMaskParams
    applyToAll: bool = True


class RemoveMaterialParams(BaseModel):
    """One ``domain.removeMaterial(material)`` call."""

    model_config = _CONFIG

    material: str = Field(min_length=1, max_length=80)
