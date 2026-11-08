"""Manifest entrypoint for the native implant plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import ImplantProcessSpec
from .variants import ImplantNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    variant = ImplantNativeVariant()
    registration = VariantRegistration(process_id=ImplantProcessSpec.id, variant=variant)
    registry.register_process(ImplantProcessSpec)
    registry.register_variant(registration)
    return registration
