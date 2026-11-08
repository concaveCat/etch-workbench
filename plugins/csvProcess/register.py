"""Manifest entrypoint for the native CSV rate-grid plugin."""

from __future__ import annotations

from process_models.v2.contract import VariantRegistration

from .spec import CsvProcessSpec
from .variants.native import CsvProcessNativeVariant

__all__ = ["register"]


def register(registry) -> VariantRegistration:
    registry.register_process(CsvProcessSpec)
    registration = VariantRegistration(process_id=CsvProcessSpec.id, variant=CsvProcessNativeVariant())
    registry.register_variant(registration)
    return registration
