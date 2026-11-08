"""Stable process identity for the native Faraday-cage ion-beam-etch plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import FaradayCageParams
from .step import parse_step

__all__ = ["FaradayCageProcessSpec"]

FaradayCageProcessSpec = ProcessSpec(
    id="faradayCage",
    version="1.0.0",
    label="Faraday-cage ion-beam etch (native)",
    params_model=FaradayCageParams,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS FaradayCageEtching driven through FaradayCageParameters "
        "(an IBEParameters struct plus cageAngle) and the project's geometry "
        "mask materials; parity with worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
