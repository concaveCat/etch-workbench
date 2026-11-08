"""Process-model plugin boundary (phase 1).

Public surface:

* :mod:`process_models.contract` - plugin/variant interface, state, result,
  descriptor, capabilities, context, and validation issues.
* :mod:`process_models.registry` - explicit ID-based registry and resolution.

Projects reference stable plugin and variant IDs only. Python module paths,
file paths, and callables never appear in project JSON.
"""

from __future__ import annotations

from .contract import (
    EngineVariant,
    Issue,
    ModelDescriptor,
    ProcessContext,
    ProcessPlugin,
    ProcessResult,
    ProcessState,
    Severity,
    VariantCapabilities,
)
from .registry import ProcessModelRegistry, ResolvedVariant

__all__ = [
    "EngineVariant",
    "Issue",
    "ModelDescriptor",
    "ProcessContext",
    "ProcessModelRegistry",
    "ProcessPlugin",
    "ProcessResult",
    "ProcessState",
    "ResolvedVariant",
    "Severity",
    "VariantCapabilities",
]
