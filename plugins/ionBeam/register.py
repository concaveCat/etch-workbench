"""Manifest entrypoint for the native ion-beam etch (IBE) plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import IonBeamProcessSpec
from .variants import IonBeamNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the ionBeam process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = IonBeamNativeVariant()
    registration = VariantRegistration(process_id=IonBeamProcessSpec.id, variant=variant)
    registry.register_process(IonBeamProcessSpec)
    registry.register_variant(registration)
    return registration
