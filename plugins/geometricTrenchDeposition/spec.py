"""Stable process identity for the native GeometricTrenchDeposition plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import GeometricTrenchDepositionParams
from .step import parse_step

__all__ = ["GeometricTrenchDepositionProcessSpec"]

GeometricTrenchDepositionProcessSpec = ProcessSpec(
    id="geometricTrenchDeposition",
    version="1.0.0",
    label="Geometric trench deposition (native)",
    params_model=GeometricTrenchDepositionParams,
    dimensions=(2, 3),
    operations=("deposit",),
    description=(
        "Native ViennaPS GeometricTrenchDeposition driven through its "
        "seven-argument constructor after duplicating the top level set as the "
        "step's depositMaterial; parity with worker/specialized.py "
        "build_specialized_model and the deposit path of the stage-8 direct "
        "reference. This is a ViennaLS geometric-advection distribution, so the "
        "process duration does not scale the deposited thickness."
    ),
    default_variant="native",
    parse_step=parse_step,
)
