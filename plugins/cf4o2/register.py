"""Manifest entrypoint for the native CF4/O2 plasma-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import CF4O2ProcessSpec
from .variants import CF4O2NativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the cf4o2 process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = CF4O2NativeVariant()
    registration = VariantRegistration(process_id=CF4O2ProcessSpec.id, variant=variant)
    registry.register_process(CF4O2ProcessSpec)
    registry.register_variant(registration)
    return registration
