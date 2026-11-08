"""Manifest entrypoint for the native isotropic plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import IsotropicProcessSpec
from .variants import IsotropicNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the isotropic process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself. Returns the registration so
    callers can inspect the declared identity without resolving again.
    """
    variant = IsotropicNativeVariant()
    registration = VariantRegistration(process_id=IsotropicProcessSpec.id, variant=variant)
    registry.register_process(IsotropicProcessSpec)
    registry.register_variant(registration)
    return registration
