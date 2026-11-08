"""Stable process identity for the native SF6/O2 plasma-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import SF6O2Params
from .step import parse_step

__all__ = ["SF6O2ProcessSpec"]

SF6O2ProcessSpec = ProcessSpec(
    id="sf6o2",
    version="1.0.0",
    label="SF6/O2 plasma etch (native)",
    params_model=SF6O2Params,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS SF6O2Etching driven through the flat eight-argument "
        "overload plus CoverageParameters; parity with "
        "worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
