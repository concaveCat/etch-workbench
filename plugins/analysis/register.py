"""Manifest entrypoint for the native cell-set analysis plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import AnalysisProcessSpec
from .variants import AnalysisNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    variant = AnalysisNativeVariant()
    registration = VariantRegistration(process_id=AnalysisProcessSpec.id, variant=variant)
    registry.register_process(AnalysisProcessSpec)
    registry.register_variant(registration)
    return registration
