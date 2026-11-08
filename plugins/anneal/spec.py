"""Stable process identity for the native anneal field plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import AnnealParams
from .step import parse_step

__all__ = ["AnnealProcessSpec"]

AnnealProcessSpec = ProcessSpec(
    id="anneal",
    version="1.0.0",
    label="Dopant diffusion and activation in a native cell set",
    params_model=AnnealParams,
    dimensions=(2, 3),
    operations=("native",),
    description=(
        "Native ViennaPS Anneal on the on-demand DenseCellSet: isothermal or "
        "scheduled temperature, Arrhenius or constant diffusivity, optional "
        "implicit solver, and solid activation. The level sets are not advanced."
    ),
    default_variant="native",
    execution_kind="field",
    parse_step=parse_step,
)
