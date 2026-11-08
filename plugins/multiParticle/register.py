"""Manifest entrypoint for the native multi-particle plasma plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import MultiParticleProcessSpec
from .variants import MultiParticleNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the multiParticle process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = MultiParticleNativeVariant()
    registration = VariantRegistration(process_id=MultiParticleProcessSpec.id, variant=variant)
    registry.register_process(MultiParticleProcessSpec)
    registry.register_variant(registration)
    return registration
