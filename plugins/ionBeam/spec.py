"""Stable process identity for the native ion-beam etch (IBE) plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import IonBeamParams
from .step import parse_step

__all__ = ["IonBeamProcessSpec"]

IonBeamProcessSpec = ProcessSpec(
    id="ionBeam",
    version="1.0.0",
    label="Ion-beam etch (native)",
    params_model=IonBeamParams,
    dimensions=(2, 3),
    operations=("etch",),
    description=(
        "Native ViennaPS IonBeamEtching driven through IBEParameters, the "
        "per-material plane-wafer-rate map, cos4 yield and geometry mask "
        "materials; parity with worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
