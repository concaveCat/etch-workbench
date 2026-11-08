"""Manifest entrypoint for the native TEOS PECVD plugin."""

from __future__ import annotations

from typing import Any

from process_models.v2.contract import VariantRegistration

from .spec import TEOSPECVDProcessSpec
from .variants import TEOSPECVDNativeVariant

__all__ = ["register"]


def register(registry: Any) -> VariantRegistration:
    """Register the teospecvd process spec and its native variant.

    The registration is just ``(process_id, variant)``: the registry derives
    the variant id/version, capabilities, engine requirement, and
    implementation hash from the variant itself.
    """
    variant = TEOSPECVDNativeVariant()
    registration = VariantRegistration(process_id=TEOSPECVDProcessSpec.id, variant=variant)
    registry.register_process(TEOSPECVDProcessSpec)
    registry.register_variant(registration)
    return registration
