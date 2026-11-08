"""Manifest entrypoint for the native HBr/O2 plasma-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import HBrO2ProcessSpec
from .variants import HBrO2NativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the hbro2 process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = HBrO2NativeVariant()
    registration = VariantRegistration(process_id=HBrO2ProcessSpec.id, variant=variant)
    registry.register_process(HBrO2ProcessSpec)
    registry.register_variant(registration)
    return registration
