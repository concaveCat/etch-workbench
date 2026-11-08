"""Stable process identity for the native selective-epitaxy plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import SelectiveEpitaxyParams
from .step import parse_step

__all__ = ["SelectiveEpitaxyProcessSpec"]

SelectiveEpitaxyProcessSpec = ProcessSpec(
    id="selectiveEpitaxy",
    version="1.0.0",
    label="Selective SiGe epitaxy (native)",
    params_model=SelectiveEpitaxyParams,
    dimensions=(2, 3),
    operations=("deposit",),
    description=(
        "Native ViennaPS SelectiveEpitaxy driven through its materialRates "
        "overload, or through the separate nvFactors overload followed by one "
        "setMaterialRate per material; parity with "
        "worker/specialized.py build_specialized_model and the deposit path of "
        "the stage-8 direct reference."
    ),
    default_variant="native",
    parse_step=parse_step,
)
