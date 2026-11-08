"""Manifest entrypoint for the native single-particle ALD plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import ALDProcessSpec
from .variants import ALDNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the ald process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = ALDNativeVariant()
    registration = VariantRegistration(process_id=ALDProcessSpec.id, variant=variant)
    registry.register_process(ALDProcessSpec)
    registry.register_variant(registration)
    return registration
