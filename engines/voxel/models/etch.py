"""Etch step models: selective top-down anisotropic, 3D isotropic, and ellipsoidal."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel.state import VoxelState, dilate_3d_euclidean


def _top_down_column_etch(
    state: VoxelState,
    rate_by_id: dict[int, float],
    duration: float,
) -> int:
    """Exact top-down line-of-sight selective etch per ``(x, y)`` column.

    Starts from the highest non-void voxel in each column and advances downward
    while consuming ``duration``. Automatically stops on any mask / etch-stop
    material absent from ``rate_by_id`` (rate == 0), while protecting buried
    features shadowed under un-etched overhangs (such as 3D NAND wordline cavities).
    """
    if duration <= 0.0 or not rate_by_id:
        return 0
    ktop = state.active_k_top()
    if ktop == 0:
        return 0

    nx, ny = state.grid.shape[0], state.grid.shape[1]
    dz = state.dz
    removed = 0

    for ix in range(nx):
        for iy in range(ny):
            col = state.grid[ix, iy, :ktop]
            rem_t = duration
            k = ktop - 1
            while k >= 0 and rem_t > 1e-12:
                # Skip open void from the top down until we hit the exposed surface:
                while k >= 0 and col[k] == 0:
                    k -= 1
                if k < 0:
                    break
                mid = int(col[k])
                rate = rate_by_id.get(mid, 0.0)
                if rate <= 0.0:
                    # Hit a mask or etch-stop material -> column is blocked!
                    break
                # Find contiguous vertical run of ``mid``
                k_run_start = k
                while k_run_start > 0 and int(col[k_run_start - 1]) == mid:
                    k_run_start -= 1
                run_cells = k - k_run_start + 1
                max_cells = rem_t * rate / dz
                etch_cells = min(run_cells, int(math.floor(max_cells + 0.5 + 1e-9)))
                if etch_cells <= 0:
                    break
                col[k - etch_cells + 1 : k + 1] = 0
                removed += etch_cells
                rem_t -= (etch_cells * dz) / rate
                k = k - etch_cells
    return removed


def _isotropic_selective_etch(
    state: VoxelState,
    rate_by_id: dict[int, float],
    duration: float,
) -> int:
    """3D isotropic selective etch propagating from gas-connected void."""
    if duration <= 0.0 or not rate_by_id:
        return 0
    max_rate = max(rate_by_id.values())
    max_disp = max_rate * duration
    if max_disp <= 0.0:
        return 0

    # Sub-step when multi-material transitions could occur across thin layers.
    step_um = max(min(state.dx, state.dy) * 1.5, 0.015)
    n_sub = max(1, min(16, int(math.ceil(max_disp / step_um))))
    dt = duration / n_sub
    removed = 0

    for _ in range(n_sub):
        ktop = min(state.grid.shape[2], state.active_k_top() + 2)
        if ktop == 0:
            break
        exposed_gas = state.exposed_void_mask(ktop)
        sub = state.grid[:, :, :ktop]
        to_clear = np.zeros_like(exposed_gas, dtype=bool)
        for mid, rate in rate_by_id.items():
            radius = rate * dt
            if radius <= 0.0:
                continue
            reach = dilate_3d_euclidean(exposed_gas, radius, state.dx, state.dy, state.dz)
            to_clear |= reach & (sub == mid)
        count = int(np.count_nonzero(to_clear))
        if count == 0:
            break
        sub[to_clear] = 0
        removed += count
    return removed


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register("etch", "anisotropic")
    def etch_anisotropic(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        duration = float(step.duration)
        split = min(max(float(step.anisotropy), 0.0), 1.0)
        dir_rates = {
            state.material_id(mat): abs(float(r)) * split
            for mat, r in step.rates.items()
            if float(r) < 0.0 and split > 0.0
        }
        iso_rates = {
            state.material_id(mat): abs(float(r)) * (1.0 - split)
            for mat, r in step.rates.items()
            if float(r) < 0.0 and (1.0 - split) > 0.0
        }
        removed = _top_down_column_etch(state, dir_rates, duration)
        if iso_rates:
            removed += _isotropic_selective_etch(state, iso_rates, duration)
        return {"removedVoxels": removed}

    @registry.register("etch", "isotropic")
    def etch_isotropic(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        rates = {
            state.material_id(mat): abs(float(r))
            for mat, r in step.rates.items()
            if float(r) < 0.0
        }
        removed = _isotropic_selective_etch(state, rates, float(step.duration))
        return {"removedVoxels": removed}

    @registry.register("etch", "ellipsoidal")
    def etch_ellipsoidal(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        duration = float(step.duration)
        raw_rates = params.get("rates") or {}
        v_rates: dict[int, float] = {}
        l_rates: dict[int, float] = {}
        for mat, pair in raw_rates.items():
            v, lat = float(pair[0]), float(pair[1])
            mid = state.material_id(mat)
            if abs(v) > 0.0:
                v_rates[mid] = abs(v)
            if abs(lat) > 0.0:
                l_rates[mid] = abs(lat)

        # Vertical front advance + tapered lateral widening on newly exposed sidewalls
        removed = _top_down_column_etch(state, v_rates, duration)
        if l_rates:
            ktop = min(state.grid.shape[2], state.active_k_top() + 1)
            if ktop > 0:
                exposed_gas = state.exposed_void_mask(ktop)
                sub = state.grid[:, :, :ktop]
                for mid, l_rate in l_rates.items():
                    lat_um = l_rate * duration * 0.5  # average taper half-width
                    rx = int(round(lat_um / state.dx))
                    ry = int(round(lat_um / state.dy))
                    if rx <= 0 and ry <= 0:
                        continue
                    widened = np.zeros_like(exposed_gas, dtype=bool)
                    for ox in range(-rx, rx + 1):
                        for oy in range(-ry, ry + 1):
                            if (ox * state.dx) ** 2 + (oy * state.dy) ** 2 <= lat_um ** 2 + 1e-15:
                                widened |= np.roll(np.roll(exposed_gas, ox, axis=0), oy, axis=1)
                    hit = widened & (sub == mid)
                    removed += int(np.count_nonzero(hit))
                    sub[hit] = 0
        return {"removedVoxels": removed}
