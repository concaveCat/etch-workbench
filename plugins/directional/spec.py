"""Stable process identity for the native directional plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import DirectionalParams
from .step import parse_step

__all__ = ["DirectionalProcessSpec"]

DirectionalProcessSpec = ProcessSpec(
    id="directional",
    version="1.0.0",
    label="Directional etch/deposition (native)",
    params_model=DirectionalParams,
    dimensions=(2, 3),
    operations=("etch", "deposit"),
    description=(
        "Native ViennaPS DirectionalProcess driven through the scalar, rate-set "
        "and material-rate overloads; parity with worker/steps/process.py."
    ),
    default_variant="native",
    parse_step=parse_step,
)
