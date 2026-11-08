"""Manifest entrypoint for the native single-particle plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import SingleParticleProcessSpec
from .variants import SingleParticleNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the singleParticle process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = SingleParticleNativeVariant()
    registration = VariantRegistration(process_id=SingleParticleProcessSpec.id, variant=variant)
    registry.register_process(SingleParticleProcessSpec)
    registry.register_variant(registration)
    return registration
