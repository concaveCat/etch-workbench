"""Stable process identity for the native CF4/O2 plasma-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import CF4O2Params
from .step import parse_step

__all__ = ["CF4O2ProcessSpec"]

CF4O2ProcessSpec = ProcessSpec(
    id="cf4o2",
    version="1.0.0",
    label="CF4/O2 plasma etch (native)",
    params_model=CF4O2Params,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS CF4O2Etching driven through the flat ten-argument "
        "overload plus CoverageParameters; parity with "
        "worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
