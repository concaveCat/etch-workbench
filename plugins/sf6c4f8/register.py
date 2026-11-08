"""Manifest entrypoint for the native SF6/C4F8 plasma-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import SF6C4F8ProcessSpec
from .variants import SF6C4F8NativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the sf6c4f8 process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = SF6C4F8NativeVariant()
    registration = VariantRegistration(process_id=SF6C4F8ProcessSpec.id, variant=variant)
    registry.register_process(SF6C4F8ProcessSpec)
    registry.register_variant(registration)
    return registration
