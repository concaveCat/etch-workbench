"""Stable process identity for the template plugin."""

from __future__ import annotations

from process_models.v2.contract import ProcessSpec

from .params import ExampleParams
from .step import parse_step

__all__ = ["ExampleSpec"]

ExampleSpec = ProcessSpec(
    id="example",
    version="0.1.0",
    label="Example process (template)",
    params_model=ExampleParams,
    dimensions=(2, 3),
    operations=("etch",),
    description="Template only: replace with the real process.",
    default_variant="native",
    parse_step=parse_step,
)
