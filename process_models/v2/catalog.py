"""Serializable execution catalog for API/UI consumers.

The catalog describes stable step identities, execution kinds, dimensions,
variants, and execution profiles. It never exposes module paths or callables.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .registry import ProcessRegistry

__all__ = ["execution_catalog", "variant_catalog"]

_CATALOG_VERSION = 1


def variant_catalog(registry: ProcessRegistry, process_id: str) -> list[dict[str, Any]]:
    """Return the JSON-safe variant descriptors for one process id."""
    return [dataclasses.asdict(descriptor) for descriptor in registry.list_variants(process_id)]


def execution_catalog(registry: ProcessRegistry) -> dict[str, Any]:
    """Return one JSON-safe catalog entry per registered process id."""
    steps: list[dict[str, Any]] = []
    counts = {"process": 0, "field": 0, "geometry": 0}
    for descriptor in registry.list_processes():
        counts[descriptor.execution_kind] = counts.get(descriptor.execution_kind, 0) + 1
        steps.append(
            {
                "id": descriptor.id,
                "version": descriptor.version,
                "label": descriptor.label,
                "executionKind": descriptor.execution_kind,
                "dimensions": list(descriptor.dimensions),
                "operations": list(descriptor.operations),
                "description": descriptor.description,
                "variants": variant_catalog(registry, descriptor.id),
            }
        )
    return {
        "schemaVersion": _CATALOG_VERSION,
        "executionKinds": counts,
        "steps": steps,
    }
