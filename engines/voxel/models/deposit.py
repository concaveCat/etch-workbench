"""Deposition step models: conformal isotropic, directional anisotropic, and fill."""

from __future__ import annotations

from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel.state import VoxelState, dilate_3d_euclidean


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register("deposit", "fill")
    def depo_fill(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        material = str(params["material"])
        offset = float(params.get("offset", 0.0))
        thickness = float(params.get("thickness", 0.0) or 0.0)
        depth = offset if offset > 0.0 else thickness
        if depth <= 0.0:
            raise ValueError(f"fill of {material!r} has non-positive offset/thickness")

        mid = state.material_id(material)
        target_z = state.surface_top_z() + depth
        k_target = state.z_to_index(target_z)
        if k_target <= 0:
            return {"fillTargetUm": round(target_z, 6)}

        # Fill every gas-connected void cell up to target_z (fills open trenches and
        # adds planar overburden to top + offset while leaving sealed voids untouched).
        exposed = state.exposed_void_mask(k_target)
        sub = state.grid[:, :, :k_target]
        sub[exposed] = mid
        return {"fillTargetUm": round(target_z, 6), "filledVoxels": int(np.count_nonzero(exposed))}

    @registry.register("deposit", "isotropic")
    def depo_isotropic(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        duration = float(step.duration)
        deposited = 0
        for material, raw_rate in step.rates.items():
            rate = float(raw_rate)
            if rate <= 0.0:
                continue
            thickness = rate * duration
            mid = state.material_id(material)
            pad_k = int(round(thickness / state.dz)) + 3
            k_lim = min(state.grid.shape[2], state.active_k_top() + pad_k)
            if k_lim <= 0:
                continue
            exposed_gas = state.exposed_void_mask(k_lim)
            solid = state.grid[:, :, :k_lim] != 0
            grown = dilate_3d_euclidean(solid, thickness, state.dx, state.dy, state.dz)
            shell = grown & exposed_gas
            state.grid[:, :, :k_lim][shell] = mid
            deposited += int(np.count_nonzero(shell))
        return {"depositedVoxels": deposited}

    @registry.register("deposit", "anisotropic")
    def depo_anisotropic(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        duration = float(step.duration)
        split = min(max(float(step.anisotropy), 0.0), 1.0)
        deposited = 0
        for material, raw_rate in step.rates.items():
            rate = float(raw_rate)
            if rate <= 0.0:
                continue
            mid = state.material_id(material)
            dir_thick = rate * split * duration
            iso_thick = rate * (1.0 - split) * duration
            if dir_thick > 0.0:
                dk = max(1, int(round(dir_thick / state.dz)))
                tops = state.column_tops_k()
                nx, ny, nz = state.grid.shape
                for ix in range(nx):
                    for iy in range(ny):
                        k0 = int(tops[ix, iy])
                        k1 = min(nz, k0 + dk)
                        if k1 > k0:
                            state.grid[ix, iy, k0:k1] = mid
                            deposited += k1 - k0
            if iso_thick > 0.0:
                pad_k = int(round(iso_thick / state.dz)) + 3
                k_lim = min(state.grid.shape[2], state.active_k_top() + pad_k)
                exposed_gas = state.exposed_void_mask(k_lim)
                solid = state.grid[:, :, :k_lim] != 0
                grown = dilate_3d_euclidean(solid, iso_thick, state.dx, state.dy, state.dz)
                shell = grown & exposed_gas
                state.grid[:, :, :k_lim][shell] = mid
                deposited += int(np.count_nonzero(shell))
        return {"depositedVoxels": deposited}
