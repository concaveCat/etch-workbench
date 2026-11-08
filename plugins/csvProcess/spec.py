"""Stable process identity for the native CSV rate-grid plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import CsvProcessParams
from .step import parse_step

__all__ = ["CsvProcessSpec"]

CsvProcessSpec = ProcessSpec(
    id="csvProcess",
    version="1.0.0",
    label="CSV rate-grid process (native)",
    params_model=CsvProcessParams,
    dimensions=(2, 3),
    operations=("native",),
    description="Native ViennaPS CSVFileProcess driven by a checked content-addressed rate table.",
    default_variant="native",
    parse_step=parse_step,
)
