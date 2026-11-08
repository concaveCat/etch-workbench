"""Manifest entrypoint for the native selective-epitaxy plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import SelectiveEpitaxyProcessSpec
from .variants import SelectiveEpitaxyNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the selectiveEpitaxy process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = SelectiveEpitaxyNativeVariant()
    registration = VariantRegistration(process_id=SelectiveEpitaxyProcessSpec.id, variant=variant)
    registry.register_process(SelectiveEpitaxyProcessSpec)
    registry.register_variant(registration)
    return registration
