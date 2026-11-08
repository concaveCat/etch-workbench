"""Manifest entrypoint for the native Faraday-cage ion-beam-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import FaradayCageProcessSpec
from .variants import FaradayCageNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the faradayCage process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = FaradayCageNativeVariant()
    registration = VariantRegistration(process_id=FaradayCageProcessSpec.id, variant=variant)
    registry.register_process(FaradayCageProcessSpec)
    registry.register_variant(registration)
    return registration
