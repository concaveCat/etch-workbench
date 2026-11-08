"""Manifest entrypoint for the template plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import ExampleSpec
from .variants import ExampleNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register ``(process_id, variant)``; the registry derives the rest."""
    registration = VariantRegistration(process_id=ExampleSpec.id, variant=ExampleNativeVariant())
    registry.register_process(ExampleSpec)
    registry.register_variant(registration)
    return registration
