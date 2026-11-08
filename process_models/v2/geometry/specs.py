"""Stable step identities for the native geometry engine.

These are ``ProcessSpec`` objects with ``execution_kind="geometry"``: they use
the same registry, resolver, executor, state backend, and plan identity as
process steps, but they are not physical process models and have no duration,
rate, flux engine, or cell set.
"""

from __future__ import annotations

from ..contract import ProcessSpec
from .params import (
    BooleanParams,
    GdsBooleanParams,
    InsertBoxParams,
    ParticleShapeParams,
    PlanarizeParams,
    RemoveMaterialParams,
    RemoveTopLevelSetParams,
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

__all__ = [
    "BooleanProcessSpec",
    "GdsBooleanProcessSpec",
    "GeometryProcessSpecs",
    "InsertBoxProcessSpec",
    "ParticleShapeProcessSpec",
    "PlanarizeProcessSpec",
    "RemoveMaterialProcessSpec",
    "RemoveTopLevelSetProcessSpec",
]

PlanarizeProcessSpec = ProcessSpec(
    id="planarize",
    version="1.0.0",
    label="Planarize at a cutoff height",
    params_model=PlanarizeParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description=(
        "Native viennaps.Planarize(domain, cutoffHeight). The cutoff is either "
        "explicit or derived from the previous exposed top of a stop material "
        "minus overPolish."
    ),
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_planarize,
)

ParticleShapeProcessSpec = ProcessSpec(
    id="particleShape",
    version="1.0.0",
    label="Native geometric box/sphere displacement",
    params_model=ParticleShapeParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description=(
        "Native BoxDistribution/SphereDistribution through "
        "ps.Process(domain, model, 0.0). Masked etch is supported; masked "
        "deposition is refused because the installed runtime segfaults."
    ),
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_particle_shape,
)

BooleanProcessSpec = ProcessSpec(
    id="boolean",
    version="1.0.0",
    label="Native level-set boolean stencil",
    params_model=BooleanParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description=(
        "Native GeometryFactory box stencil (2D/3D) or vertical cylinder "
        "stencil (3D) applied with Domain.applyBooleanOperation."
    ),
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_boolean,
)

GdsBooleanProcessSpec = ProcessSpec(
    id="gdsBoolean",
    version="1.0.0",
    label="GDS-mask boolean",
    params_model=GdsBooleanParams,
    dimensions=(3,),
    operations=("geometry",),
    description=(
        "Boolean with a 3D operand built from one selected GDS layer/datatype. "
        "The run supplies the verified GDS operand provider through "
        "ProcessContext.assets; the geometry engine applies the native boolean."
    ),
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_gds_boolean,
)

RemoveMaterialProcessSpec = ProcessSpec(
    id="removeMaterial",
    version="1.0.0",
    label="Remove one native material",
    params_model=RemoveMaterialParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description="Native domain.removeMaterial(material).",
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_remove_material,
)

InsertBoxProcessSpec = ProcessSpec(
    id="insertBox",
    version="1.0.0",
    label="Insert a native box level set as a material",
    params_model=InsertBoxParams,
    dimensions=(3,),
    operations=("geometry",),
    description=(
        "Native ps.LevelSet + ps.MakeGeometry(ps.Box(min, max)) + domain.insertNextLevelSetAsMaterial(box, material)."
    ),
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_insert_box,
)

RemoveTopLevelSetProcessSpec = ProcessSpec(
    id="removeTopLevelSet",
    version="1.0.0",
    label="Remove the top native level set",
    params_model=RemoveTopLevelSetParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description="Native domain.removeTopLevelSet().",
    default_variant="native",
    execution_kind="geometry",
    parse_step=parse_remove_top_level_set,
)

GeometryProcessSpecs = (
    PlanarizeProcessSpec,
    ParticleShapeProcessSpec,
    BooleanProcessSpec,
    GdsBooleanProcessSpec,
    RemoveMaterialProcessSpec,
    InsertBoxProcessSpec,
    RemoveTopLevelSetProcessSpec,
)
