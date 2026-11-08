"""Stable process identity for the native TEOS deposition plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import TEOSDepositionParams
from .step import parse_step

__all__ = ["TEOSDepositionProcessSpec"]

TEOSDepositionProcessSpec = ProcessSpec(
    id="teosDeposition",
    version="1.0.0",
    label="TEOS thermal deposition (native)",
    params_model=TEOSDepositionParams,
    dimensions=(2, 3),
    operations=("deposit",),
    description=(
        "Native ViennaPS TEOSDeposition driven through its six-argument "
        "constructor after duplicating the top level set as SiO2; parity with "
        "worker/specialized.py build_specialized_model and the deposit path of "
        "the stage-8 direct reference."
    ),
    default_variant="native",
    parse_step=parse_step,
)
