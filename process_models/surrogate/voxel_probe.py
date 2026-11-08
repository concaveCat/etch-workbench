"""Proof surrogate plugin for the per-step level-set <-> voxel adapter.

This plugin is deliberately tiny: it exists to prove that the voxel adapter in
:mod:`process_models.voxel` can be driven end to end from the plugin contract,
not to model a physical process. Only height-field geometries are supported
(the implementation erodes one horizontal material layer at a time).

The variant never mutates ``state.domain``. It reads a :class:`VoxelGrid`,
optionally edits the grid, and asks :func:`process_models.voxel.write_voxels`
for a freshly built level-set domain.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ..contract import (
    EngineVariant,
    Issue,
    ModelDescriptor,
    ProcessContext,
    ProcessPlugin,
    ProcessResult,
    ProcessState,
    VariantCapabilities,
)
from ..voxel import VoxelGrid, read_voxels, write_voxels

VOID = -1
MAX_LAYERS = 8
ADAPTER = "height-field-v1"

Operation = Literal["identity", "erode_top_layer"]


class VoxelProbeParams(BaseModel):
    """Parameters for the proof surrogate.

    ``layers`` is schema-bounded to ``1..8``; :meth:`VoxelProbeVariant.validate`
    repeats the check so a caller that bypasses schema validation still gets an
    ``Issue`` instead of a surprise at execution time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Operation
    layers: int = Field(default=1, ge=1, le=MAX_LAYERS)


class VoxelProbeVariant(EngineVariant):
    """Apply an identity or top-layer erosion on a voxel round trip."""

    variant_id = "voxel_probe"
    version = "0.1.0"
    capabilities = VariantCapabilities(
        dimensions=[2, 3],
        engines=["surrogate"],
        supports_fields=True,
        supports_masks=True,
        experimental=True,
    )

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        issues: list[Issue] = []
        if ctx.dimension not in (2, 3):
            issues.append(
                Issue(
                    path="dimension",
                    message=f"voxel_probe supports dimensions 2 and 3, not {ctx.dimension}",
                )
            )
        if getattr(params, "operation", None) == "erode_top_layer":
            layers = int(getattr(params, "layers", 1))
            if layers > MAX_LAYERS:
                issues.append(
                    Issue(
                        path="layers",
                        message=f"erode_top_layer supports at most {MAX_LAYERS} layers, got {layers}",
                    )
                )
        return issues

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext) -> ProcessResult:
        operation: str = getattr(params, "operation", "identity")
        layers = int(getattr(params, "layers", 1))

        grid = read_voxels(state.domain)
        removed_cells = 0
        warnings: list[str] = []

        if operation == "erode_top_layer":
            material = np.array(grid.material, dtype=np.int32, copy=True)
            removed_cells = _erode_top_layers(material, layers)
            if removed_cells == 0:
                warnings.append("erode_top_layer removed no material cells; geometry left unchanged")
            grid = VoxelGrid(
                material=material,
                origin=grid.origin,
                spacing=grid.spacing,
                material_order=grid.material_order,
                fields=grid.fields,
            )
        else:
            warnings.append("identity operation: surface is round-tripped through the voxel grid unchanged")

        new_domain = write_voxels(state.domain, grid)

        return ProcessResult(
            domain=new_domain,
            metadata={
                "operation": operation,
                "layers": layers,
                "voxelShape": list(grid.material.shape),
                "materialOrder": list(grid.material_order),
                "removedCells": removed_cells,
                "adapter": ADAPTER,
            },
            warnings=warnings,
        )


class VoxelProbePlugin(ProcessPlugin):
    """Stable identity of the proof surrogate."""

    id = "voxel_probe"
    version = "0.1.0"
    params_model = VoxelProbeParams
    variants = [VoxelProbeVariant()]

    def describe(self) -> ModelDescriptor:
        return ModelDescriptor(
            id=self.id,
            label="Voxel probe (experimental)",
            version=self.version,
            kind="surrogate",
            operation="geometry",
            dimensions=[2, 3],
            fields=["voxel"],
            notes=(
                "Proof plugin for the per-step level-set <-> voxel adapter; "
                "height-field geometries only; not a production process model."
            ),
        )


def _erode_top_layers(material: np.ndarray, layers: int) -> int:
    """Clear the topmost occupied z slices of ``material`` in place.

    The last axis is the vertical (z) index for both 2D and 3D grids. Each
    removed slice is recomputed from the current grid so an eroded layer does
    not leave floating material behind. The grid is never emptied completely:
    the last remaining material cell is kept.
    """
    if material.ndim == 0 or material.size == 0:
        return 0

    z_axis = material.ndim - 1
    removed = 0

    for _ in range(layers):
        occupied = np.argwhere(material != VOID)
        if occupied.size == 0:
            break

        top = int(occupied[:, z_axis].max())
        selector = [slice(None)] * material.ndim
        selector[z_axis] = top
        index = tuple(selector)

        occupied_before = int(np.count_nonzero(material != VOID))
        to_remove = int(np.count_nonzero(material[index] != VOID))
        if to_remove == 0 or occupied_before - to_remove < 1:
            break

        material[index] = np.where(material[index] == VOID, material[index], VOID)
        removed += to_remove

    return removed
