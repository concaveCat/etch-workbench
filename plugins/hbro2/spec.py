"""Stable process identity for the native HBr/O2 plasma-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import HBrO2Params
from .step import parse_step

__all__ = ["HBrO2ProcessSpec"]

HBrO2ProcessSpec = ProcessSpec(
    id="hbro2",
    version="1.0.0",
    label="HBr/O2 plasma etch (native)",
    params_model=HBrO2Params,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS HBrO2Etching driven through the flat eight-argument "
        "overload plus CoverageParameters; parity with "
        "worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
