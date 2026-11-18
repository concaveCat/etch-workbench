"""Lithography patterning (sharp polygon vs Gaussian aerial image) and selective strip."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel import native
from engines.voxel.state import VoxelState


def _apply_coat(
    state: VoxelState,
    in_poly: np.ndarray,
    step: StepIR,
    polygon_count: int,
    extra_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = step.parameters or {}
    layer = int(params["layer"])
    polarity = str(params.get("polarity", "negative")).lower()
    thickness = float(params["thickness"])
    coat = float(params.get("coat", 0.1))
    material = str(params.get("material", "PMMA"))

    mid = state.material_id(material)
    z_base = state.surface_top_z()
    notes = dict(extra_notes or {})

    if polarity == "negative":
        eff_coat = coat if coat > 0.0 else thickness
        k0 = state.z_to_index(z_base)
        k1 = state.z_to_index(z_base + eff_coat)
        if k1 > k0:
            if native.is_available():
                native.litho_coat_inplace(state.grid, in_poly, k0, k1, mid, negative=True)
            else:
                exposed = state.exposed_void_mask(k1)
                coat_zone = np.zeros_like(exposed, dtype=bool)
                coat_zone[:, :, k0:k1] = (~in_poly)[:, :, None]
                fill_mask = exposed & coat_zone
                state.grid[:, :, :k1][fill_mask] = mid
        return {
            "layer": layer,
            "polarity": polarity,
            "polygons": polygon_count,
            "resistTopUm": round(z_base + eff_coat, 6),
            **notes,
        }

    k0 = state.z_to_index(z_base)
    k1 = state.z_to_index(z_base + thickness)
    if k1 > k0:
        if native.is_available():
            native.litho_coat_inplace(state.grid, in_poly, k0, k1, mid, negative=False)
        else:
            sub = state.grid[:, :, k0:k1]
            place = in_poly[:, :, None] & (sub == 0)
            sub[place] = mid
    return {
        "layer": layer,
        "polarity": polarity,
        "polygons": polygon_count,
        "resistTopUm": round(z_base + thickness, 6),
        **notes,
    }


def _periodic_gaussian_blur_2d(
    mask_2d: np.ndarray, sigma_um: float, dx: float, dy: float
) -> np.ndarray:
    """Separable 2D periodic Gaussian convolution on ``[nx, ny]`` in float32."""
    arr = mask_2d.astype(np.float32)
    if sigma_um <= 0.0:
        return arr

    rx = min(arr.shape[0] // 2, max(1, int(math.ceil(3.0 * sigma_um / max(dx, 1e-6)))))
    ry = min(arr.shape[1] // 2, max(1, int(math.ceil(3.0 * sigma_um / max(dy, 1e-6)))))

    ox = np.arange(-rx, rx + 1, dtype=np.float32) * np.float32(dx)
    kx = np.exp(-0.5 * (ox / np.float32(sigma_um)) ** 2)
    kx /= np.sum(kx)

    oy = np.arange(-ry, ry + 1, dtype=np.float32) * np.float32(dy)
    ky = np.exp(-0.5 * (oy / np.float32(sigma_um)) ** 2)
    ky /= np.sum(ky)

    tmp = np.zeros_like(arr)
    for shift, weight in zip(range(-rx, rx + 1), kx):
        tmp += float(weight) * np.roll(arr, shift, axis=0)

    out = np.zeros_like(arr)
    for shift, weight in zip(range(-ry, ry + 1), ky):
        out += float(weight) * np.roll(tmp, shift, axis=1)
    return out


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register(
        "mask",
        "litho",
        model_id="voxel.litho.hard_polygon",
        algorithm_type="first_order_approx",
        fidelity="approximate",
        origin="Jordan Ray-Crossing Binary Polygon Rasterization + Aligned SPX 0.1 um Negative Coat",
        summary="按 GDS 原始多边形做硬边界二值光栅化（90° 直角，无光学圆角），涂覆 coat 厚光刻胶并显影开孔。",
    )
    def mask_litho_hard(state: VoxelState, step: StepIR, ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        layer = int(params["layer"])
        polygons = ctx.layer_polygons(layer)
        if not polygons:
            raise NotImplementedError(f"litho layer {layer} has no polygons in window")
        in_poly = state.rasterize_polygons(polygons)
        return _apply_coat(state, in_poly, step, len(polygons))

    @registry.register_model(
        "voxel.litho.gaussian_aerial",
        slot="mask/litho",
        algorithm_type="optical_aerial",
        fidelity="empirical",
        origin="Hopkins First-Order Gaussian Aerial-Image Blur + 0.5 Resist Threshold",
        summary="对二维掩膜做可分离周期高斯模糊（默认 sigma=10 nm）后再按 threshold=0.5 二值化，模拟光学衍射拐角圆化。",
        aliases=("litho_gaussian",),
    )
    def mask_litho_gaussian(
        state: VoxelState, step: StepIR, ctx: StepModelContext
    ) -> dict[str, Any]:
        params = step.parameters or {}
        layer = int(params["layer"])
        sigma_um = float(params.get("sigma", 0.010))
        threshold = float(params.get("threshold", 0.5))
        polygons = ctx.layer_polygons(layer)
        if not polygons:
            raise NotImplementedError(f"litho layer {layer} has no polygons in window")
        raw_mask = state.rasterize_polygons(polygons)
        aerial = _periodic_gaussian_blur_2d(raw_mask, sigma_um, state.dx, state.dy)
        in_poly = aerial >= np.float32(threshold)
        return _apply_coat(
            state,
            in_poly,
            step,
            len(polygons),
            extra_notes={"sigmaUm": sigma_um, "threshold": threshold},
        )

    @registry.register(
        "strip",
        "strip",
        model_id="voxel.strip.exact_id_clear",
        algorithm_type="exact_morphology",
        fidelity="exact",
        origin="Exact Volumetric Material-ID Set Subtraction",
        summary="将指定材料 ID 的所有体素精确清零还原为气相 void。",
    )
    def strip_materials(state: VoxelState, step: StepIR, _ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        names = [str(n) for n in (params.get("materials") or [])]
        if not names:
            raise NotImplementedError("strip step lists no materials")
        ktop = state.active_k_top()
        removed = 0
        if ktop > 0:
            mids = [state.name_to_id[name] for name in names if name in state.name_to_id]
            if mids:
                if native.is_available():
                    removed = native.strip_materials_inplace(state.grid, ktop, mids)
                else:
                    sub = state.grid[:, :, :ktop]
                    for mid in mids:
                        hit = sub == mid
                        removed += int(np.count_nonzero(hit))
                        sub[hit] = 0
        return {"strippedMaterials": names, "removedVoxels": removed}
