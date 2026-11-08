"""Manifest entrypoint for the native directional plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import DirectionalProcessSpec
from .variants import DirectionalNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the directional process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = DirectionalNativeVariant()
    registration = VariantRegistration(process_id=DirectionalProcessSpec.id, variant=variant)
    registry.register_process(DirectionalProcessSpec)
    registry.register_variant(registration)
    return registration
