"""Etch step models: selective column etch, 3D isotropic erosion, and cylindrical/conical ellipsoidal."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel import native
from engines.voxel.state import VoxelState, dilate_3d_euclidean


def _top_down_column_etch(
    state: VoxelState,
    rate_by_id: dict[int, float],
    duration: float,
) -> int:
    """Exact top-down line-of-sight selective etch per ``(x, y)`` column."""
    if duration <= 0.0 or not rate_by_id:
        return 0
    ktop = state.active_k_top()
    if ktop == 0:
        return 0
    if native.is_available():
        return native.top_down_etch_inplace(state.grid, ktop, rate_by_id, duration, state.dz)

    nx, ny = state.grid.shape[0], state.grid.shape[1]
    dz = state.dz
    removed = 0

    for ix in range(nx):
        for iy in range(ny):
            col = state.grid[ix, iy, :ktop]
            rem_t = duration
            k = ktop - 1
            while k >= 0 and rem_t > 1e-12:
                while k >= 0 and col[k] == 0:
                    k -= 1
                if k < 0:
                    break
                mid = int(col[k])
                rate = rate_by_id.get(mid, 0.0)
                if rate <= 0.0:
                    break
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

    step_um = max(min(state.dx, state.dy) * 1.5, 0.015)
    n_sub = max(1, min(16, int(math.ceil(max_disp / step_um))))
    dt = duration / n_sub
    removed = 0
    use_native = native.is_available()

    for _ in range(n_sub):
        ktop = min(state.grid.shape[2], state.active_k_top() + 2)
        if ktop == 0:
            break
        if use_native:
            count = native.etch_isotropic_substep_inplace(
                state.grid, ktop, rate_by_id, dt, state.dx, state.dy, state.dz
            )
            if count == 0:
                break
            removed += count
            continue

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


def _parse_ellipsoidal_rates(
    state: VoxelState, step: StepIR
) -> tuple[dict[int, float], dict[int, float]]:
    raw_rates = (step.parameters or {}).get("rates") or {}
    v_rates: dict[int, float] = {}
    l_rates: dict[int, float] = {}
    for mat, pair in raw_rates.items():
        v, lat = float(pair[0]), float(pair[1])
        mid = state.material_id(mat)
        if abs(v) > 0.0:
            v_rates[mid] = abs(v)
        if abs(lat) > 0.0:
            l_rates[mid] = abs(lat)
    return v_rates, l_rates


def _widen_slice_2d(
    gas_2d: np.ndarray, radius_um: float, dx: float, dy: float
) -> np.ndarray:
    if radius_um <= 0.0 or not np.any(gas_2d):
        return gas_2d.copy()
    if native.is_available():
        # 1-slice 3D dilation with huge dz equals exact 2D periodic Euclidean disk dilation!
        return native.dilate_3d(gas_2d[:, :, None], radius_um, dx, dy, radius_um * 10.0 + 1.0)[:, :, 0]
    rx = int(round(radius_um / dx))
    ry = int(round(radius_um / dy))
    widened = gas_2d.copy()
    for ox in range(-rx, rx + 1):
        for oy in range(-ry, ry + 1):
            if (ox * dx) ** 2 + (oy * dy) ** 2 <= radius_um ** 2 + 1e-15:
                widened |= np.roll(np.roll(gas_2d, ox, axis=0), oy, axis=1)
    return widened


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register(
        "etch",
        "anisotropic",
        model_id="voxel.etch.line_of_sight_column",
        algorithm_type="ray_column",
        fidelity="exact",
        origin="1D Top-Down Line-of-Sight Column Integration (+ Lie-Trotter Isotropic Split if anisotropy < 1)",
        summary="逐 (x, y) 列自顶向下按时间预算积分去除速率，自动止于选择比停止层并保护悬空阴影区。",
    )
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

    @registry.register(
        "etch",
        "isotropic",
        model_id="voxel.etch.substepped_fh_erosion",
        algorithm_type="exact_morphology",
        fidelity="exact",
        origin="Sub-Stepped 3D Euclidean Ball Erosion via Felzenszwalb-Huttenlocher Exact EDT",
        summary="从连通暴露气相向四周做精确三维欧氏球腐蚀（跨材料按 <=15 nm 位移切分子步）。",
    )
    def etch_isotropic(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        rates = {
            state.material_id(mat): abs(float(r))
            for mat, r in step.rates.items()
            if float(r) < 0.0
        }
        removed = _isotropic_selective_etch(state, rates, float(step.duration))
        return {"removedVoxels": removed}

    @registry.register(
        "etch",
        "ellipsoidal",
        model_id="voxel.etch.cylindrical_halfwidth",
        algorithm_type="first_order_approx",
        fidelity="approximate",
        origin="Vertical Column Cut + Uniform Half-Width (0.5 * r_lat * t) Horizontal Disk Widening",
        summary="先按垂向速率直刻到底，再将暴露侧壁按平均半宽 0.5 * r_lat * t 统一水平扩孔（直筒近似）。",
    )
    def etch_ellipsoidal_halfwidth(
        state: VoxelState, step: StepIR, _ctx: StepModelContext
    ) -> dict[str, Any]:
        duration = float(step.duration)
        v_rates, l_rates = _parse_ellipsoidal_rates(state, step)
        removed = _top_down_column_etch(state, v_rates, duration)
        if l_rates:
            ktop = min(state.grid.shape[2], state.active_k_top() + 1)
            if ktop > 0:
                if native.is_available():
                    removed += native.widen_2d_clear_inplace(
                        state.grid, ktop, l_rates, duration, state.dx, state.dy
                    )
                else:
                    exposed_gas = state.exposed_void_mask(ktop)
                    sub = state.grid[:, :, :ktop]
                    for mid, l_rate in l_rates.items():
                        lat_um = l_rate * duration * 0.5
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

    @registry.register_model(
        "voxel.etch.linear_depth_taper",
        slot="etch/ellipsoidal",
        algorithm_type="geometric_envelope",
        fidelity="approximate",
        origin="Depth-Interpolated Conical Taper Envelope (Top Full r_lat*t -> Etch Floor 0)",
        summary="按实际刻蚀深度区间线性插值侧向扩孔半径（槽口扩 r_lat*t，槽底扩 0），生成上宽下窄的连续锥形侧壁。",
        aliases=("ellipsoidal_taper",),
    )
    def etch_ellipsoidal_taper(
        state: VoxelState, step: StepIR, _ctx: StepModelContext
    ) -> dict[str, Any]:
        duration = float(step.duration)
        v_rates, l_rates = _parse_ellipsoidal_rates(state, step)
        tops_before = state.column_tops_k()
        removed = _top_down_column_etch(state, v_rates, duration)
        if not l_rates:
            return {"removedVoxels": removed}

        tops_after = state.column_tops_k()
        etched_cols = tops_after < tops_before
        if not np.any(etched_cols):
            return {"removedVoxels": removed}

        k_low = int(np.min(tops_after[etched_cols]))
        k_high = int(np.max(tops_before[etched_cols]))
        if k_high <= k_low:
            return {"removedVoxels": removed}

        ktop = min(state.grid.shape[2], max(state.active_k_top(), k_high) + 1)
        exposed_gas = state.exposed_void_mask(ktop)

        n_bands = min(12, max(1, k_high - k_low))
        edges = np.linspace(k_low, k_high, n_bands + 1, dtype=int)
        for mid, l_rate in l_rates.items():
            full_lat = l_rate * duration
            if full_lat <= 0.0:
                continue
            for b in range(n_bands):
                ka, kb = int(edges[b]), int(edges[b + 1])
                if kb <= ka:
                    continue
                frac = ((b + 0.5) / n_bands)
                r_band = full_lat * frac
                if r_band < min(state.dx, state.dy) * 0.45:
                    continue
                # Use mid-band exposed gas footprint for 2D periodic disk widening
                k_mid = min(ktop - 1, (ka + kb) // 2)
                widened_2d = _widen_slice_2d(exposed_gas[:, :, k_mid], r_band, state.dx, state.dy)
                band_view = state.grid[:, :, ka:kb]
                hit = widened_2d[:, :, None] & (band_view == mid)
                cnt = int(np.count_nonzero(hit))
                if cnt > 0:
                    band_view[hit] = 0
                    removed += cnt
        return {"removedVoxels": removed, "taperBands": n_bands}
