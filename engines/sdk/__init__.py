"""SDK for building pluggable simulation engines and per-process step models.

Engine authors inherit :class:`EngineAdapterBase` for lifecycle/IO/timing and
register modular process-step kernels in a :class:`StepModelRegistry`. Adding a new
physics/geometry approximation for one specific process step (for example a custom
Damascene CMP dishing model or tapered contact etch) is a single decorated function
with zero scheduler, CLI, or UI changes.
"""

from engines.sdk.models import (
    AlgorithmMetadata,
    StepModelContext,
    StepModelFn,
    StepModelRegistry,
)
from engines.sdk.runner import EngineAdapterBase, run_adapter_cli

__all__ = [
    "AlgorithmMetadata",
    "EngineAdapterBase",
    "StepModelContext",
    "StepModelFn",
    "StepModelRegistry",
    "run_adapter_cli",
]
