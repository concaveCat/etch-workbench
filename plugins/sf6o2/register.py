"""Manifest entrypoint for the native SF6/O2 plasma-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import SF6O2ProcessSpec
from .variants import SF6O2NativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the sf6o2 process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = SF6O2NativeVariant()
    registration = VariantRegistration(process_id=SF6O2ProcessSpec.id, variant=variant)
    registry.register_process(SF6O2ProcessSpec)
    registry.register_variant(registration)
    return registration
