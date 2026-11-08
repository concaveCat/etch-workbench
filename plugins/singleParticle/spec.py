"""Stable process identity for the native single-particle plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import SingleParticleParams
from .step import parse_step

__all__ = ["SingleParticleProcessSpec"]

SingleParticleProcessSpec = ProcessSpec(
    id="singleParticle",
    version="1.0.0",
    label="Single-particle plasma etch/deposit (native)",
    params_model=SingleParticleParams,
    dimensions=(2, 3),
    operations=("etch", "deposit"),
    description=(
        "Native ViennaPS SingleParticleProcess with operation-signed uniform or "
        "per-material rates, deterministic ray flux, and native advection; "
        "parity with worker/steps/process.py."
    ),
    default_variant="native",
    parse_step=parse_step,
)
