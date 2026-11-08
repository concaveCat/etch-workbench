"""Manifest entrypoint for the native TEOS deposition plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import TEOSDepositionProcessSpec
from .variants import TEOSDepositionNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the teosDeposition process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = TEOSDepositionNativeVariant()
    registration = VariantRegistration(process_id=TEOSDepositionProcessSpec.id, variant=variant)
    registry.register_process(TEOSDepositionProcessSpec)
    registry.register_variant(registration)
    return registration
