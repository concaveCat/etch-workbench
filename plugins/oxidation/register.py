"""Manifest entrypoint for the native silicon thermal-oxidation plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import OxidationProcessSpec
from .variants import OxidationNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the oxidation process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = OxidationNativeVariant()
    registration = VariantRegistration(process_id=OxidationProcessSpec.id, variant=variant)
    registry.register_process(OxidationProcessSpec)
    registry.register_variant(registration)
    return registration
