"""Stable process identity for the native silicon thermal-oxidation plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import OxidationParams
from .step import parse_step

__all__ = ["OxidationProcessSpec"]

OxidationProcessSpec = ProcessSpec(
    id="oxidation",
    version="1.0.0",
    label="Silicon thermal oxidation (native)",
    params_model=OxidationParams,
    dimensions=(2, 3),
    operations=("native",),
    description=(
        "Native ViennaPS Oxidation driven through its no-argument constructor "
        "and setters, with the process duration derived from timeHours; parity "
        "with worker/specialized.py build_specialized_model. The mask and "
        "advancedOxidation paths stay on the worker."
    ),
    default_variant="native",
    parse_step=parse_step,
)
