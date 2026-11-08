"""Stable process identity for the native single-particle ALD plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import ALDParams
from .step import parse_step

__all__ = ["ALDProcessSpec"]

ALDProcessSpec = ProcessSpec(
    id="ald",
    version="1.0.0",
    label="Single-particle ALD (native)",
    params_model=ALDParams,
    dimensions=(2, 3),
    operations=("deposit",),
    description=(
        "Native ViennaPS SingleParticleALD driven through "
        "SingleParticleALDParams plus AtomicLayerProcessParameters after "
        "duplicating the top level set as the step's depositMaterial; parity "
        "with worker/specialized.py build_specialized_model and the deposit "
        "path of the stage-8 direct reference. Surface diffusion stays on the "
        "old worker path because the 4.7.0 stencil can hang."
    ),
    default_variant="native",
    parse_step=parse_step,
)
