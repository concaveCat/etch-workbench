"""Manifest entrypoint for the native GeometricTrenchDeposition plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import GeometricTrenchDepositionProcessSpec
from .variants import GeometricTrenchDepositionNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the geometricTrenchDeposition spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = GeometricTrenchDepositionNativeVariant()
    registration = VariantRegistration(process_id=GeometricTrenchDepositionProcessSpec.id, variant=variant)
    registry.register_process(GeometricTrenchDepositionProcessSpec)
    registry.register_variant(registration)
    return registration
