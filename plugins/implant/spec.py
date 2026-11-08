"""Stable process identity for the native implant field plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import ImplantParams
from .step import parse_step

__all__ = ["ImplantProcessSpec"]

ImplantProcessSpec = ProcessSpec(
    id="implant",
    version="1.0.0",
    label="Ion implantation into a native cell set",
    params_model=ImplantParams,
    dimensions=(2, 3),
    operations=("native",),
    description=(
        "Native ViennaPS IonImplantation with a manual Pearson IV/dual Pearson "
        "IV/channeling profile and optional Hobler damage. The process runs on "
        "an on-demand DenseCellSet; the level sets are never advanced."
    ),
    default_variant="native",
    execution_kind="field",
    parse_step=parse_step,
)
