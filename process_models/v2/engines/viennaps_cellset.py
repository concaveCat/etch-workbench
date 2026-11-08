"""ViennaPS implementation of the v2 cell-set field domain.

The native ``DenseCellSet`` is authoritative while a field sequence runs.  A
snapshot converts it to the generic :class:`CellSetState` (dense grid, int32
material IDs, float64 fields); rehydration re-generates the native cell set
from the current ``Domain`` and copies the stored fields back.  No native
cell-set file is written by this module and the level sets are never advanced.

The native generator widens a **copy** of the level-set domain before calling
``generateCellSet``.  The canonical ``ProcessState.geometry`` handle is
therefore untouched by cell-set generation; the copy only provides the wider
substrate band the dense grid needs.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..cellset import (
    CELLSET_CONVERSION,
    CellSetBackendError,
    CellSetCapabilities,
    CellSetConfig,
)
from ..contract import ProcessContext
from ..state import CellSetState, Field, GridSpec, LevelSetState

__all__ = ["ViennaPSCellSetBackend", "NATIVE_CELL_SET_LIMIT"]

#: Hard construction limit shared with the schema-7 worker path.
NATIVE_CELL_SET_LIMIT = 2_000_000

_NATIVE_CLASS = "viennaps.DenseCellSet"
_COVER_GAS = "GAS"
_MATERIAL_LABEL = "Material"
_FILLING_LABEL = "FillingFraction"


class ViennaPSCellSetBackend:
    """Native ``DenseCellSet`` lifecycle for an installed ViennaPS module."""

    id = "viennaps-dense-cellset"
    version = "1.0.0"
    capabilities = CellSetCapabilities(
        conversions=frozenset({CELLSET_CONVERSION}),
        sparse=False,
    )

    def __init__(self, ps: Any, *, max_cells: int = NATIVE_CELL_SET_LIMIT) -> None:
        if ps is None:
            raise CellSetBackendError("ViennaPSCellSetBackend requires the ViennaPS module")
        max_cells = int(max_cells)
        if max_cells <= 0:
            raise ValueError(f"max_cells must be positive, got {max_cells!r}")
        self._ps = ps
        self._max_cells = max_cells

    # -- public protocol -------------------------------------------------

    def geometry_digest(self, geometry: LevelSetState) -> str:
        """Return a stable digest of the exposed surface and material order."""
        domain = geometry.handle
        try:
            mesh = domain.getSurfaceMesh(
                addInterfaces=True,
                sharpCorners=False,
                minNodeDistanceFactor=0.01,
            )
            points = mesh.getNodes()
        except (AttributeError, RuntimeError, TypeError) as error:
            raise CellSetBackendError(f"cannot read the native surface for a geometry digest: {error}") from error
        if not points:
            raise CellSetBackendError("native surface has no nodes; cannot digest an empty geometry")

        dimension = int(geometry.dimension)
        rounded: list[tuple[float, float, float]] = []
        for point in points:
            rounded.append(
                (
                    round(float(point[0]), 9),
                    round(float(point[1]), 9),
                    round(float(point[2]), 9) if dimension == 3 else 0.0,
                )
            )
        rounded.sort()

        digest = hashlib.sha256()
        digest.update(struct.pack("<Bdd", dimension, float(geometry.grid_delta), float(geometry.grid_delta)))
        for x, y, z in rounded:
            digest.update(struct.pack("<ddd", x, y, z))
        digest.update(struct.pack("<I", int(domain.getNumberOfLevelSets())))
        mapping = getattr(domain, "getMaterialMap", None)
        if callable(mapping):
            material_map = mapping()
            for index in range(int(material_map.size())):
                digest.update(struct.pack("<i", int(material_map.getMaterialIdAtIdx(index))))
        return digest.hexdigest()

    def ensure(
        self,
        geometry: LevelSetState,
        context: ProcessContext,
        config: CellSetConfig,
        *,
        source_step_id: str | None = None,
    ) -> CellSetState:
        """Generate the native cell set and return its first snapshot."""
        field_domain, native = self._generate(geometry, context, config)
        return self.snapshot(
            native,
            context,
            geometry=geometry,
            config=config,
            source_step_id=source_step_id,
            domain_handle=field_domain,
        )

    def snapshot(
        self,
        native_cell_set: Any,
        context: ProcessContext,
        *,
        base: CellSetState | None = None,
        geometry: LevelSetState | None = None,
        config: CellSetConfig | None = None,
        source_step_id: str | None = None,
        domain_handle: Any = None,
    ) -> CellSetState:
        """Convert the authoritative native cell set to a persistable state."""
        grid = self._grid_from_native(native_cell_set, dimension=int(context.dimension))
        cells = int(native_cell_set.getNumberOfCells())
        material_ids = self._material_ids(native_cell_set, grid, cells)
        fields = self._fields(native_cell_set, grid, cells)

        provenance: dict[str, Any] = dict(base.provenance) if base is not None else {}
        effective = config
        if effective is None and base is not None:
            effective = CellSetConfig.from_state(base)
        if effective is not None:
            provenance.update(effective.provenance())
        elif geometry is None:
            raise CellSetBackendError(
                "cell-set snapshot needs either a CellSetConfig, a snapshot base with "
                "height/substrateDepth provenance, or the source geometry"
            )
        if geometry is not None:
            provenance["sourceGeometryDigest"] = self.geometry_digest(geometry)
        if source_step_id is not None:
            provenance["sourceStepId"] = str(source_step_id)
        provenance.update(
            {
                "nativeClass": _NATIVE_CLASS,
                "nativeConstructor": "Domain.generateCellSet",
                "arrayOrder": grid.array_order,
                "cellOrder": "native-x-fastest",
                "numberOfCells": cells,
                "schemaVersion": 1,
            }
        )

        return CellSetState(
            grid=grid,
            material_ids=material_ids,
            fields=fields,
            provenance=provenance,
            handle=native_cell_set,
            domain_handle=domain_handle if domain_handle is not None else (base.domain_handle if base else None),
        )

    def rehydrate(
        self,
        geometry: LevelSetState,
        state: CellSetState,
        context: ProcessContext,
        config: CellSetConfig,
    ) -> CellSetState:
        """Recreate the native handle from a snapshot and the current geometry."""
        stored_digest = state.provenance.get("sourceGeometryDigest")
        digest = self.geometry_digest(geometry)
        if stored_digest is not None and str(stored_digest) != digest:
            raise CellSetBackendError(
                "cell-set snapshot belongs to a different geometry "
                f"(stored {stored_digest}, current {digest}); field states are not remapped "
                "across a geometry change in this version"
            )
        if _MATERIAL_LABEL in state.fields:
            raise CellSetBackendError(
                "CellSetState.fields must not contain the reserved native label 'Material'; "
                "material IDs belong in CellSetState.material_ids"
            )

        field_domain, native = self._generate(geometry, context, config)
        grid = self._grid_from_native(native, dimension=int(context.dimension))
        self._require_same_grid(state.grid, grid)
        cells = int(native.getNumberOfCells())

        observed_material = self._material_ids(native, grid, cells)
        if state.material_ids is not None:
            expected = np.asarray(state.material_ids)
            if expected.shape != observed_material.shape:
                raise CellSetBackendError(
                    f"stored material_ids shape {expected.shape} does not match the regenerated grid "
                    f"{observed_material.shape}"
                )
            if not np.array_equal(expected.astype(np.int32), observed_material):
                raise CellSetBackendError(
                    "regenerated native material IDs do not match the stored snapshot; "
                    "the geometry and cell-set config are not the same lattice state"
                )

        observed_filling = self._filling_fractions(native, grid, cells)
        for name, field in state.fields.items():
            if int(field.components) != 1:
                raise CellSetBackendError(
                    f"field {name!r} has {field.components} components; the native snapshot path "
                    "only rehydrates scalar fields"
                )
            if not isinstance(field.data, np.ndarray):
                raise CellSetBackendError(f"field {name!r} data is {type(field.data).__name__}, not a numpy array")
            values = field.data
            if name == _FILLING_LABEL:
                if not np.allclose(values, observed_filling, rtol=0.0, atol=1e-12):
                    raise CellSetBackendError("regenerated native FillingFraction does not match the stored snapshot")
                continue
            flat = values.reshape(-1, order=state.grid.array_order).astype(np.float64, copy=False)
            if flat.size != cells:
                raise CellSetBackendError(f"field {name!r} has {flat.size} values for a {cells}-cell native cell set")
            if name not in set(str(label) for label in native.getScalarDataLabels()):
                native.addScalarData(name, 0.0)
            native.setScalarData(name, flat.tolist())

        provenance = dict(state.provenance)
        provenance["sourceGeometryDigest"] = digest
        provenance.update(config.provenance())
        return CellSetState(
            grid=state.grid,
            material_ids=state.material_ids,
            fields=state.fields,
            provenance=provenance,
            handle=native,
            domain_handle=field_domain,
        )

    def validate(
        self,
        state: CellSetState,
        context: ProcessContext,
        config: CellSetConfig | None = None,
    ) -> None:
        """Check that a snapshot still agrees with its native handle and config."""
        if int(state.grid.dimension) != int(context.dimension):
            raise CellSetBackendError(
                f"cell-set grid is {state.grid.dimension}D but the request context is {context.dimension}D"
            )
        if abs(float(state.grid.spacing) - float(context.grid_delta)) > 1e-12 * max(
            1.0, abs(float(context.grid_delta))
        ):
            raise CellSetBackendError(
                f"cell-set grid spacing {state.grid.spacing!r} does not match request grid delta {context.grid_delta!r}"
            )
        expected_cells = math.prod(int(value) for value in state.grid.shape)
        if "numberOfCells" in state.provenance and int(state.provenance["numberOfCells"]) != expected_cells:
            raise CellSetBackendError("cell-set provenance numberOfCells does not match the dense grid shape product")
        if state.handle is not None:
            observed = int(state.handle.getNumberOfCells())
            if observed != expected_cells:
                raise CellSetBackendError(
                    f"native cell set has {observed} cells but the dense grid describes {expected_cells}; "
                    "sparse native cell sets are not supported by this version"
                )
        if config is not None and "height" in state.provenance:
            stored = CellSetConfig.from_state(state)
            if stored is not None and not stored.compatible_with(config):
                raise CellSetBackendError(
                    "cell-set snapshot was created with a different config: "
                    f"stored {stored.provenance()}, requested {config.provenance()}"
                )

    # -- native helpers --------------------------------------------------

    def _generate(self, geometry: LevelSetState, context: ProcessContext, config: CellSetConfig) -> tuple[Any, Any]:
        domain = geometry.handle
        dimension = int(geometry.dimension)
        if dimension != int(context.dimension):
            raise CellSetBackendError(f"geometry is {dimension}D but the request context is {context.dimension}D")
        delta = float(domain.getGridDelta())
        if abs(delta - float(geometry.grid_delta)) > 1e-12 * max(1.0, abs(delta)):
            raise CellSetBackendError(
                f"native grid delta {delta!r} does not match the state grid delta {geometry.grid_delta!r}"
            )
        if config.height + 1e-12 < delta:
            raise CellSetBackendError(
                f"cellSet.height {config.height:g} is below one grid interval ({delta:g}); "
                "the native DenseCellSet generator would crash"
            )

        surface_top, surface_bottom = self._surface_bounds(domain, dimension=dimension)
        top_node = math.ceil(surface_top / delta - 1e-9) * delta
        if config.height + 1e-9 < top_node:
            raise CellSetBackendError(
                f"cellSet.height {config.height:g} is below the highest surface point "
                f"({surface_top:g}); Domain.generateCellSet takes the absolute top coordinate"
            )
        band_cells = 2 * int(math.ceil(float(config.substrate_depth) / delta)) + 2
        if band_cells < 2:
            raise CellSetBackendError("cellSet.substrateDepth is below one grid interval")
        self._check_allocation_budget(
            domain,
            context,
            config,
            delta=delta,
            surface_bottom=surface_bottom,
            band_cells=band_cells,
        )

        # Widen only a deep copy: every level set in the simulated domain must
        # stay byte-identical for later process steps and replay.
        field_domain = self._ps.Domain(domain)
        for level_set in field_domain.getLevelSets():
            self._ps.ls.Expand(level_set, band_cells).apply()

        cover = self._cover_material(config.cover_material)
        field_domain.generateCellSet(float(config.height), cover, True)
        native = field_domain.getCellSet()
        if native is None:
            raise CellSetBackendError("Domain.generateCellSet did not store a native DenseCellSet")

        cells = int(native.getNumberOfCells())
        if cells <= 0 or cells > self._max_cells:
            raise CellSetBackendError(
                f"native DenseCellSet has {cells} cells, outside the supported range "
                f"1..{self._max_cells:,}; reduce cellSet.height/substrateDepth or coarsen gridDelta"
            )
        native.buildNeighborhood()
        is3d = dimension == 3
        native.setPeriodicBoundary(
            [bool(config.periodic), bool(config.periodic), False] if is3d else [bool(config.periodic), False]
        )
        return field_domain, native

    def _check_allocation_budget(
        self,
        domain: Any,
        context: ProcessContext,
        config: CellSetConfig,
        *,
        delta: float,
        surface_bottom: float,
        band_cells: int,
    ) -> None:
        """Refuse an obviously oversized grid before the native allocation.

        The final check still runs on the measured cell count, but this guard
        prevents a bad ``height``/``substrateDepth`` from asking the native
        generator for hundreds of millions of cells first.
        """
        dimension = int(context.dimension)
        box = context.project.get("domain") if isinstance(context.project, Mapping) else None
        extents: list[float] = []
        if isinstance(box, Mapping):
            for key in ("xExtent", "yExtent"):
                value = box.get(key)
                if value is not None:
                    extents.append(float(value))
        if len(extents) < dimension:
            mesh = domain.getSurfaceMesh(
                addInterfaces=True,
                sharpCorners=False,
                minNodeDistanceFactor=0.01,
            )
            points = mesh.getNodes()
            extents = []
            for axis in range(dimension):
                values = [float(point[axis]) for point in points]
                extents.append(max(values) - min(values))
        lateral = 1
        for extent in extents[:dimension]:
            lateral *= max(1, int(math.ceil(extent / delta)) + 2)
        bottom = math.floor(surface_bottom / delta + 1e-9) * delta - (band_cells // 2) * delta
        rows = max(2, int(math.ceil((float(config.height) - bottom) / delta)) + 4)
        bound = lateral * rows
        guard = int(math.ceil(bound * 2.5))
        if guard > self._max_cells:
            raise CellSetBackendError(
                f"cell-set grid needs about {bound:,} cells between {bottom:g} and "
                f"{float(config.height):g} ({guard:,} with band safety margin), above the "
                f"{self._max_cells:,}-cell limit; reduce cellSet.height/substrateDepth or "
                "coarsen gridDelta"
            )

    def _surface_bounds(self, domain: Any, *, dimension: int) -> tuple[float, float]:
        try:
            mesh = domain.getSurfaceMesh(
                addInterfaces=True,
                sharpCorners=False,
                minNodeDistanceFactor=0.01,
            )
            points = mesh.getNodes()
        except (AttributeError, RuntimeError, TypeError) as error:
            raise CellSetBackendError(f"cannot read the native surface for cell-set generation: {error}") from error
        axis = 2 if dimension == 3 else 1
        values = [float(point[axis]) for point in points]
        if not values:
            raise CellSetBackendError("native surface has no nodes; cannot size the cell set")
        if any(not math.isfinite(value) for value in values):
            raise CellSetBackendError("native surface bounds contain a non-finite coordinate")
        return max(values), min(values)

    def _cover_material(self, name: str) -> Any:
        if name == _COVER_GAS:
            return self._ps.Material.GAS
        registry = self._ps.MaterialRegistry.instance()
        material = registry.findMaterial(name)
        if material is None:
            raise CellSetBackendError(f"cover material {name!r} is absent from the native MaterialRegistry")
        return registry.getMaterial(name)

    def _grid_from_native(self, native: Any, *, dimension: int) -> GridSpec:
        nodes = native.getNodes()
        if not nodes:
            raise CellSetBackendError("native DenseCellSet has no nodes")
        delta = float(native.getGridDelta())
        if not (delta > 0.0) or not math.isfinite(delta):
            raise CellSetBackendError(f"native DenseCellSet has an invalid grid delta {delta!r}")
        cells = int(native.getNumberOfCells())

        origin: list[float] = []
        shape: list[int] = []
        for axis in range(dimension):
            lattice = sorted({int(round(float(node[axis]) / delta)) for node in nodes})
            if len(lattice) < 2:
                raise CellSetBackendError(f"native DenseCellSet has no extent along axis {axis}")
            gaps = [right - left for left, right in zip(lattice, lattice[1:])]
            if any(gap != 1 for gap in gaps):
                raise CellSetBackendError(
                    "native DenseCellSet is not a contiguous regular lattice along "
                    f"axis {axis}; sparse grids are not supported by this version"
                )
            origin.append(lattice[0] * delta)
            shape.append(lattice[-1] - lattice[0])

        grid = GridSpec(
            dimension=dimension,
            origin=tuple(origin),
            spacing=delta,
            shape=tuple(shape),
            array_order="F",
        )
        expected = math.prod(grid.shape)
        if expected != cells:
            raise CellSetBackendError(
                f"native DenseCellSet reports {cells} cells but its bounding lattice describes "
                f"{expected} ({grid.shape}); sparse native cell sets are not supported by this version"
            )
        if cells > self._max_cells:
            raise CellSetBackendError(
                f"native DenseCellSet has {cells} cells, above the supported {self._max_cells:,}-cell limit"
            )
        return grid

    @staticmethod
    def _material_ids(native: Any, grid: GridSpec, cells: int) -> np.ndarray:
        values = np.asarray(native.getScalarData(_MATERIAL_LABEL), dtype=np.float64)
        if values.size != cells:
            raise CellSetBackendError(f"native Material field has {values.size} values for {cells} cells")
        if not np.all(np.isfinite(values)):
            raise CellSetBackendError("native Material field contains a non-finite value")
        rounded = np.rint(values)
        if not np.allclose(values, rounded, rtol=0.0, atol=1e-9):
            raise CellSetBackendError("native Material field contains non-integer material IDs")
        return rounded.astype(np.int32).reshape(grid.shape, order=grid.array_order)

    def _fields(self, native: Any, grid: GridSpec, cells: int) -> dict[str, Field]:
        fields: dict[str, Field] = {}
        for raw_label in native.getScalarDataLabels():
            label = str(raw_label)
            if label == _MATERIAL_LABEL:
                continue
            if label == _FILLING_LABEL:
                values = np.asarray(native.getFillingFractions(), dtype=np.float64)
                unit = "1"
            else:
                values = np.asarray(native.getScalarData(label), dtype=np.float64)
                unit = "1/cm3"
            if values.size != cells:
                raise CellSetBackendError(f"native scalar field {label!r} has {values.size} values for {cells} cells")
            if not np.all(np.isfinite(values)):
                raise CellSetBackendError(f"native scalar field {label!r} contains a non-finite value")
            fields[label] = Field(
                name=label,
                unit=unit,
                association="cell",
                dtype="float64",
                shape=grid.shape,
                data=values.reshape(grid.shape, order=grid.array_order),
            )
        return fields

    def _filling_fractions(self, native: Any, grid: GridSpec, cells: int) -> np.ndarray:
        values = np.asarray(native.getFillingFractions(), dtype=np.float64)
        if values.size != cells:
            raise CellSetBackendError(f"native FillingFraction has {values.size} values for {cells} cells")
        return values.reshape(grid.shape, order=grid.array_order)

    @staticmethod
    def _require_same_grid(stored: GridSpec, observed: GridSpec) -> None:
        if stored.dimension != observed.dimension:
            raise CellSetBackendError(
                f"stored cell-set grid is {stored.dimension}D but the regenerated grid is {observed.dimension}D"
            )
        if stored.shape != observed.shape:
            raise CellSetBackendError(
                f"stored cell-set shape {stored.shape} does not match the regenerated shape "
                f"{observed.shape}; the geometry changed and this version does not remap fields"
            )
        if abs(float(stored.spacing) - float(observed.spacing)) > 1e-12 * max(1.0, abs(float(observed.spacing))):
            raise CellSetBackendError(
                f"stored cell-set spacing {stored.spacing!r} does not match the regenerated "
                f"spacing {observed.spacing!r}"
            )
        for axis in range(stored.dimension):
            if abs(float(stored.origin[axis]) - float(observed.origin[axis])) > 1e-9 * max(
                1.0, abs(float(observed.origin[axis]))
            ):
                raise CellSetBackendError(
                    f"stored cell-set origin {stored.origin!r} does not match the regenerated "
                    f"origin {observed.origin!r}; the geometry changed and this version does not remap fields"
                )
