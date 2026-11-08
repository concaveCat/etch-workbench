"""v2 port of the experimental voxel-probe surrogate plugin.

The plugin exists to prove that the per-step level-set <-> voxel adapter in
:mod:`process_models.voxel` can be driven through the v2 contract: the resolver
checks capabilities, the ``DefaultStateAdapter`` converts geometry to voxels and
back, and the variant itself only edits a :class:`~process_models.voxel.VoxelGrid`.

Ownership of the conversion stays in the adapter. The variant never calls
:func:`process_models.voxel.read_voxels` or
:func:`process_models.voxel.write_voxels` and never mutates the input state; it
returns a :class:`StepResult` whose ``delta`` carries the new ``voxels`` entry
the adapter commits.

Only height-field geometries are supported: the erosion model removes one
horizontal material slice at a time and cannot express re-entrant shapes.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...voxel import VoxelGrid
from ..contract import (
    EngineVariant,
    Issue,
    ProcessContext,
    ProcessSpec,
    VariantCapabilities,
    VariantRegistration,
)
from ..engine import EngineLease, EngineRequirement
from ..state import ProcessState, StateDelta, StepResult, VoxelState

__all__ = [
    "ADAPTER",
    "MAX_LAYERS",
    "VOID",
    "VoxelProbeParams",
    "VoxelProbeProcessSpec",
    "VoxelProbeSpec",
    "VoxelProbeVariant",
    "register_voxel_probe",
]

#: Native ``viennaps`` material id used for void cells in a voxel grid.
VOID = -1

#: Upper schema bound for ``VoxelProbeParams.layers``.
MAX_LAYERS = 8

#: Identity of the level-set <-> voxel mapping used by this plugin.
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
    """Apply an identity or top-layer erosion on a voxel round trip.

    The variant reads ``state.voxels`` (materialised by the state adapter) and
    writes a new ``VoxelState`` into the returned :class:`StepResult`'s
    ``delta``. Witness metadata records the effective operation, grid
    shape, material order, and how many cells the erosion removed.
    """

    variant_id = "voxel_probe"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2, 3),
        reads=frozenset({"geometry", "voxels"}),
        writes=frozenset({"voxels"}),
        geometry_constraints=("height-field",),
        supports_fields=False,
        supports_masks=True,
        experimental=True,
        mutates_input=False,
        determinism="strict",
    )
    engine = EngineRequirement(kind="surrogate", engine_id="python", min_version="3.12")
    implementation_hash: str | None = None

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Issue]:
        issues: list[Issue] = []
        if ctx.dimension not in self.capabilities.dimensions:
            issues.append(
                Issue(
                    path="dimension",
                    message=f"voxel_probe supports dimensions 2 and 3, not {ctx.dimension}",
                )
            )

        layers = int(getattr(params, "layers", 1))
        if not 1 <= layers <= MAX_LAYERS:
            issues.append(
                Issue(
                    path="layers",
                    message=f"layers must be within 1..{MAX_LAYERS}, got {layers}",
                )
            )
        return issues

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        # Surrogates run on the Python interpreter that hosts this process, so
        # the lease is accepted for the uniform v2 signature and left unused.
        del engine

        operation: str = str(getattr(params, "operation", "identity"))
        layers = int(getattr(params, "layers", 1))

        voxels = state.require("voxels")
        grid: VoxelGrid = voxels.grid

        removed_cells = 0
        warnings: list[str] = []
        if operation == "erode_top_layer":
            material = np.array(grid.material, dtype=np.int32, copy=True)
            removed_cells = _erode_top_layers(material, layers)
            if removed_cells == 0:
                warnings.append("erode_top_layer removed no material cells; geometry left unchanged")
            new_grid = VoxelGrid(
                material=material,
                origin=grid.origin,
                spacing=grid.spacing,
                material_order=grid.material_order,
                fields=grid.fields,
            )
        else:
            new_grid = grid
            warnings.append("identity operation: surface is round-tripped through the voxel grid unchanged")

        return StepResult(
            delta=StateDelta(voxels=VoxelState(grid=new_grid, source=ADAPTER)),
            metadata={
                "operation": operation,
                "layers": layers,
                "voxelShape": list(new_grid.material.shape),
                "materialOrder": list(new_grid.material_order),
                "removedCells": removed_cells,
                "adapter": ADAPTER,
            },
            warnings=warnings,
        )


VoxelProbeProcessSpec = ProcessSpec(
    id="voxel_probe",
    version="1.0.0",
    label="Voxel probe (experimental)",
    params_model=VoxelProbeParams,
    dimensions=(2, 3),
    operations=("geometry",),
    description=(
        "Proof plugin for the per-step level-set <-> voxel adapter; "
        "height-field geometries only; not a production process model."
    ),
)

#: Alias matching the short name used in the v2 spec.
VoxelProbeSpec = VoxelProbeProcessSpec


def register_voxel_probe(registry: Any) -> VariantRegistration:
    """Register the process spec and its single variant in ``registry``.

    Returns the registration so callers can inspect the resolved identity
    without resolving again.
    """
    variant = VoxelProbeVariant()
    registration = VariantRegistration(process_id=VoxelProbeProcessSpec.id, variant=variant)
    registry.register_process(VoxelProbeProcessSpec)
    registry.register_variant(registration)
    return registration


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
