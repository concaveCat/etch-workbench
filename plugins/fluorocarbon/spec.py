"""Stable process identity for the native fluorocarbon plasma-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import FluorocarbonParams
from .step import parse_step

__all__ = ["FluorocarbonProcessSpec"]

FluorocarbonProcessSpec = ProcessSpec(
    id="fluorocarbon",
    version="1.0.0",
    label="Fluorocarbon plasma etch (native)",
    params_model=FluorocarbonParams,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS FluorocarbonEtching driven through the "
        "FluorocarbonParameters object overload plus the per-material "
        "parameter table; parity with worker/specialized.py "
        "build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
