"""Manifest entrypoint for the native anneal plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import AnnealProcessSpec
from .variants import AnnealNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    variant = AnnealNativeVariant()
    registration = VariantRegistration(process_id=AnnealProcessSpec.id, variant=variant)
    registry.register_process(AnnealProcessSpec)
    registry.register_variant(registration)
    return registration
