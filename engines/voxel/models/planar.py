"""Planarisation / CMP models, plus a custom Damascene dishing model example."""

from __future__ import annotations

from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel import native
from engines.voxel.state import VoxelState


def _material_max_z(state: VoxelState, material: str) -> float | None:
    mid = state.name_to_id.get(material)
    if mid is None:
        return None
    ktop = state.active_k_top()
    if ktop == 0:
        return None
    if native.is_available():
        best_k = native.material_max_k(state.grid, ktop, mid)
        if best_k <= 0:
            return None
        return state.z_min + best_k * state.dz
    z_has = np.any(state.grid[:, :, :ktop] == mid, axis=(0, 1))
    idx = np.flatnonzero(z_has)
    if idx.size == 0:
        return None
    return state.z_min + (int(idx[-1]) + 1) * state.dz


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register("planarize", "cmp")
    def planarize_cmp(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        stop = params.get("stop_material")
        if stop:
            stop_top = _material_max_z(state, str(stop))
            if stop_top is None:
                return {"cmpNoOp": True, "reason": f"stop material {stop!r} absent"}
            cutoff = stop_top + float(params.get("offset", 0.0))
        elif "cutoff" in params:
            cutoff = float(params["cutoff"])
        else:
            raise NotImplementedError("cmp step requires stop_material or cutoff")

        k_cut = state.z_to_index(cutoff)
        ktop = state.active_k_top()
        removed = 0
        if ktop > k_cut:
            if native.is_available():
                removed = native.cmp_clear_above_inplace(state.grid, k_cut, ktop)
            else:
                removed = int(np.count_nonzero(state.grid[:, :, k_cut:ktop]))
                state.grid[:, :, k_cut:ktop] = 0
        return {"cutoffUm": round(cutoff, 6), "removedVoxels": removed}

    @registry.register_model("cmp_dishing")
    def cmp_dishing(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        """Process-specific custom model: CMP with metal dishing inside trenches."""
        base = planarize_cmp(state, step, _ctx)
        if base.get("cmpNoOp"):
            return base
        params = step.parameters or {}
        metal = str(params.get("metal_material", "Cu"))
        max_dish = float(params.get("max_dishing_um", 0.008))
        mid = state.name_to_id.get(metal)
        if mid is None or max_dish <= 0.0:
            return {**base, "dishedVoxels": 0}

        k_cut = state.z_to_index(float(base["cutoffUm"]))
        if k_cut <= 0:
            return {**base, "dishedVoxels": 0}
        surface_is_metal = state.grid[:, :, k_cut - 1] == mid
        dish_cells = max(1, int(round(max_dish / state.dz)))
        k0 = max(0, k_cut - dish_cells)
        dished = 0
        for k in range(k0, k_cut):
            hit = surface_is_metal & (state.grid[:, :, k] == mid)
            dished += int(np.count_nonzero(hit))
            state.grid[:, :, k][hit] = 0
        return {**base, "dishedVoxels": dished, "maxDishingUm": max_dish}
