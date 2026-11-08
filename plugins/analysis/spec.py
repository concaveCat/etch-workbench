"""Stable process identity for the native cell-set analysis plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import AnalysisParams
from .step import parse_step

__all__ = ["AnalysisProcessSpec"]

AnalysisProcessSpec = ProcessSpec(
    id="analysis",
    version="1.0.0",
    label="Native junction and sheet-resistance analysis",
    params_model=AnalysisParams,
    dimensions=(2, 3),
    operations=("native",),
    description=(
        "Native ViennaPS NetDoping and SheetResistance on the on-demand "
        "DenseCellSet. NetDoping's output field stays in the cell set; "
        "measurements are emitted as OutputSet metrology. Geometry is unchanged."
    ),
    default_variant="native",
    execution_kind="field",
    parse_step=parse_step,
)
