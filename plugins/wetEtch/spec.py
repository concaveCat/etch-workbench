"""Stable process identity for the native crystalline wet-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import WetEtchParams
from .step import parse_step

__all__ = ["WetEtchProcessSpec"]

WetEtchProcessSpec = ProcessSpec(
    id="wetEtch",
    version="1.0.0",
    label="Crystalline wet etch (native)",
    params_model=WetEtchParams,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS WetEtching driven through either constructor overload: "
        "material-rate only, or the crystalline direction/rate overload; parity "
        "with worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
