"""Surrogate (Python) plugin implementations for the v2 contract.

Only the experimental :class:`VoxelProbeVariant` lives here so far. It is a
proof plugin for the per-step level-set <-> voxel adapter, not a production
process model.
"""

from __future__ import annotations

from .voxel_probe import (
    VoxelProbeParams,
    VoxelProbeProcessSpec,
    VoxelProbeSpec,
    VoxelProbeVariant,
    register_voxel_probe,
)

__all__ = [
    "VoxelProbeParams",
    "VoxelProbeProcessSpec",
    "VoxelProbeSpec",
    "VoxelProbeVariant",
    "register_voxel_probe",
]
