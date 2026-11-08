"""Stable process identity for the native oxide-regrowth plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import OxideRegrowthParams
from .step import parse_step

__all__ = ["OxideRegrowthProcessSpec"]

OxideRegrowthProcessSpec = ProcessSpec(
    id="oxideRegrowth",
    version="1.0.0",
    label="Oxide regrowth and redeposition (native)",
    params_model=OxideRegrowthParams,
    dimensions=(2, 3),
    operations=("native",),
    description=(
        "Native ViennaPS OxideRegrowth driven through its twelve-argument "
        "constructor after the worker's DenseCellSet derivation; parity with "
        "worker/specialized.py build_specialized_model. The process duration "
        "comes from the step's explicit duration."
    ),
    default_variant="native",
    parse_step=parse_step,
)
