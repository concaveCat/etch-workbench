"""Lithography patterning and selective material strip models."""

from __future__ import annotations

from typing import Any

import numpy as np

from engines.protocol import StepIR
from engines.sdk.models import StepModelContext, StepModelRegistry
from engines.voxel import native
from engines.voxel.state import VoxelState


def register_models(registry: StepModelRegistry[VoxelState]) -> None:
    @registry.register("mask", "litho")
    def mask_litho(state: VoxelState, step: StepIR, ctx: StepModelContext) -> dict[str, Any]:
        params = step.parameters or {}
        layer = int(params["layer"])
        polarity = str(params.get("polarity", "negative")).lower()
        thickness = float(params["thickness"])
        coat = float(params.get("coat", 0.1))
        material = str(params.get("material", "PMMA"))

        polygons = ctx.layer_polygons(layer)
        if not polygons:
            raise NotImplementedError(f"litho layer {layer} has no polygons in window")

        in_poly = state.rasterize_polygons(polygons)
        mid = state.material_id(material)
        z_base = state.surface_top_z()

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
                "polygons": len(polygons),
                "resistTopUm": round(z_base + eff_coat, 6),
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
            "polygons": len(polygons),
            "resistTopUm": round(z_base + thickness, 6),
        }

    @registry.register("strip", "strip")
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
