"""Stable process identity for the native isotropic plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import IsotropicParams
from .step import parse_step

__all__ = ["IsotropicProcessSpec"]

IsotropicProcessSpec = ProcessSpec(
    id="isotropic",
    version="1.0.0",
    label="Isotropic etch/deposit (native)",
    params_model=IsotropicParams,
    dimensions=(2, 3),
    operations=("etch", "deposit"),
    description=(
        "Native ViennaPS IsotropicProcess driven through the worker rate and "
        "advection helpers; parity with worker/steps/process.py."
    ),
    default_variant="native",
    parse_step=parse_step,
)
