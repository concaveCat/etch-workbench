"""Manifest entrypoint for the native oxide-regrowth plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import OxideRegrowthProcessSpec
from .variants import OxideRegrowthNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the oxideRegrowth process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = OxideRegrowthNativeVariant()
    registration = VariantRegistration(process_id=OxideRegrowthProcessSpec.id, variant=variant)
    registry.register_process(OxideRegrowthProcessSpec)
    registry.register_variant(registration)
    return registration
