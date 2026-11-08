"""Native geometry operations as first-class execution steps.

The operations are ordinary :class:`~process_models.v2.contract.ProcessSpec`
instances with ``execution_kind="geometry"``. They reuse the same registry,
resolver, executor, engine lease, and persistence as process steps, but one
shared :class:`GeometryEngine` implements every operation and each operation
keeps its own parameter model.
"""

from __future__ import annotations

from .engine import GeometryEngine, GeometryEngineError
from .params import (
    BooleanParams,
    GdsBooleanMaskParams,
    GdsBooleanParams,
    InsertBoxParams,
    ParticleShapeParams,
    PlanarizeParams,
    RemoveMaterialParams,
    RemoveTopLevelSetParams,
)
from .register import register_geometry
from .specs import (
    BooleanProcessSpec,
    GdsBooleanProcessSpec,
    GeometryProcessSpecs,
    InsertBoxProcessSpec,
    ParticleShapeProcessSpec,
    PlanarizeProcessSpec,
    RemoveMaterialProcessSpec,
    RemoveTopLevelSetProcessSpec,
)
from .step import (
    parse_boolean,
    parse_gds_boolean,
    parse_insert_box,
    parse_particle_shape,
    parse_planarize,
    parse_remove_material,
    parse_remove_top_level_set,
)
from .variants import GeometryVariant

__all__ = [
    "BooleanParams",
    "BooleanProcessSpec",
    "GdsBooleanMaskParams",
    "GdsBooleanParams",
    "GdsBooleanProcessSpec",
    "GeometryEngine",
    "GeometryEngineError",
    "GeometryProcessSpecs",
    "GeometryVariant",
    "InsertBoxParams",
    "InsertBoxProcessSpec",
    "ParticleShapeParams",
    "ParticleShapeProcessSpec",
    "PlanarizeParams",
    "PlanarizeProcessSpec",
    "RemoveMaterialParams",
    "RemoveMaterialProcessSpec",
    "RemoveTopLevelSetParams",
    "RemoveTopLevelSetProcessSpec",
    "parse_boolean",
    "parse_gds_boolean",
    "parse_insert_box",
    "parse_particle_shape",
    "parse_planarize",
    "parse_remove_material",
    "parse_remove_top_level_set",
    "register_geometry",
]
