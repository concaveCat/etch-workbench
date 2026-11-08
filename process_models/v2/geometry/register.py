"""Registration of the built-in geometry operations into a ProcessRegistry."""

from __future__ import annotations

from typing import Any

from ..contract import VariantRegistration
from .engine import GeometryEngine
from .specs import GeometryProcessSpecs
from .variants import GeometryVariant

__all__ = ["register_geometry"]


def register_geometry(registry: Any, *, engine: GeometryEngine | None = None) -> tuple[VariantRegistration, ...]:
    """Register every built-in geometry operation exactly once.

    Geometry operations share one :class:`GeometryEngine` instance. They are
    ordinary step specs with ``execution_kind="geometry"`` so the existing
    resolver/executor/storage path is reused; callers that only want physical
    processes can ignore these registrations or filter on ``execution_kind``.
    """
    geometry = engine or GeometryEngine()
    registrations: list[VariantRegistration] = []
    for spec in GeometryProcessSpecs:
        try:
            existing = registry.get_process(spec.id)
        except KeyError:
            pass
        else:
            # Registration is idempotent for callers that reuse a registry,
            # but a same-named physical process is a real conflict.
            if existing.execution_kind != "geometry":
                raise ValueError(
                    f"process id {spec.id!r} is already registered with execution_kind "
                    f"{existing.execution_kind!r}, not 'geometry'"
                )
            continue
        registry.register_process(spec)
        registration = VariantRegistration(
            process_id=spec.id,
            variant=GeometryVariant(spec.id, geometry),
        )
        registry.register_variant(registration)
        registrations.append(registration)
    return tuple(registrations)
