"""Stable process identity for the native multi-particle plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import MultiParticleParams
from .step import parse_step

__all__ = ["MultiParticleProcessSpec"]

MultiParticleProcessSpec = ProcessSpec(
    id="multiParticle",
    version="1.0.0",
    label="Multi-particle plasma etch/deposit (native)",
    params_model=MultiParticleParams,
    dimensions=(2, 3),
    operations=("etch", "deposit"),
    description=(
        "Native ViennaPS MultiParticleProcess with up to eight ion/neutral "
        "species and a fixed weighted-sum rate table; CPU_DISK/CPU_TRIANGLE "
        "only. The GPU multi-ion split strategy is deferred."
    ),
    default_variant="native",
    parse_step=parse_step,
)
