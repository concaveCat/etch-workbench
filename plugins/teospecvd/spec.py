"""Stable process identity for the native TEOS PECVD plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import TEOSPECVDParams
from .step import parse_step

__all__ = ["TEOSPECVDProcessSpec"]

TEOSPECVDProcessSpec = ProcessSpec(
    id="teospecvd",
    version="1.0.0",
    label="TEOS PECVD SiO2 deposition (native)",
    params_model=TEOSPECVDParams,
    dimensions=(2, 3),
    operations=("deposit",),
    description=(
        "Native ViennaPS TEOSPECVD driven through the flat eight-argument "
        "overload; always deposits SiO2 on a new top level set, matching "
        "worker/specialized.py build_specialized_model."
    ),
    default_variant="native",
    parse_step=parse_step,
)
