"""Stable process identity for the native SF6/C4F8 plasma-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import SF6C4F8Params
from .step import parse_step

__all__ = ["SF6C4F8ProcessSpec"]

SF6C4F8ProcessSpec = ProcessSpec(
    id="sf6c4f8",
    version="1.0.0",
    label="SF6/C4F8 plasma etch (native)",
    params_model=SF6C4F8Params,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS SF6C4F8Etching driven through the flat six-argument "
        "overload plus CoverageParameters; parity with "
        "worker/specialized.py build_specialized_model. No passivation particle."
    ),
    default_variant="native",
    parse_step=parse_step,
)
