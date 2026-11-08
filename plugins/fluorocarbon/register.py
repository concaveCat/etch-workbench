"""Manifest entrypoint for the native fluorocarbon plasma-etch plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import FluorocarbonProcessSpec
from .variants import FluorocarbonNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the fluorocarbon process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = FluorocarbonNativeVariant()
    registration = VariantRegistration(process_id=FluorocarbonProcessSpec.id, variant=variant)
    registry.register_process(FluorocarbonProcessSpec)
    registry.register_variant(registration)
    return registration
