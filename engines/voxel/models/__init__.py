"""Built-in per-process step models for the volumetric ``voxel`` engine."""

from __future__ import annotations

from engines.sdk.models import StepModelRegistry
from engines.voxel.models import deposit, etch, litho, planar
from engines.voxel.state import VoxelState


def build_default_registry() -> StepModelRegistry[VoxelState]:
    registry: StepModelRegistry[VoxelState] = StepModelRegistry("voxel")
    deposit.register_models(registry)
    etch.register_models(registry)
    litho.register_models(registry)
    planar.register_models(registry)
    return registry
