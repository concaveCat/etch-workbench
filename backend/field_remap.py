"""Conservative cell-grid remap for the native field ``DenseCellSet``.

The 4.7.0 implant/anneal/analysis models operate on a native ``DenseCellSet``
that is generated from the level sets. The grid is axis aligned and lives on
the domain lattice (spacing ``gridDelta``), so it can be described exactly by
an origin, a cell count per axis and the grid spacing.

A GDS / multi-material geometry does **not** produce a full box: some cells in
the bounding lattice are inactive (cover/void regions where no cell exists), so
the native cell count is smaller than ``nx * ny * nz``. The grid is therefore
represented as a bounding lattice plus an *active map* keyed by the absolute
integer cell coordinate ``(ix, iy, iz)``. Scalar arrays are indexed by the
native cell order; the active map translates that order to lattice keys.

When a surface-moving step runs between two field steps the level sets change
and the cell set must be regenerated. The scalar fields are **not**
redistributed: a new active cell either maps to exactly one old active cell
(same absolute lattice key) or is new and starts at ``0``. Active cells that
disappear, and cells that leave the solid (their new material is the cover
material), lose their dopant and the loss is recorded per label. No
interpolation and no nearest-neighbour smoothing is performed.

This module is pure Python so the mapping can be unit tested without a native
runtime; the worker only performs the native calls and feeds the resulting
arrays back in.
"""
from __future__ import annotations

import hashlib
import math
from array import array
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: Cell scalar labels the native generator owns. They are always rebuilt from
#: the current level sets and never remapped.
NATIVE_SYSTEM_LABELS: tuple[str, ...] = ("Material", "FillingFraction")

#: Vertical cell axis per dimension (the 2D grid stores x/depth, 3D x/y/z).
VERTICAL_AXIS: dict[int, int] = {2: 1, 3: 2}


class GridRemapError(RuntimeError):
    """The two grids cannot be remapped without interpolation."""


def _collapse(values: Iterable[float], delta: float) -> list[int]:
    """Return the sorted, de-duplicated lattice coordinates of ``values``."""
    scale = delta if delta > 0.0 else 1.0
    return sorted({round(float(value) / scale) for value in values})


@dataclass(frozen=True)
class CellGrid:
    """Bounding lattice plus the active-cell map of a native ``DenseCellSet``.

    ``shape`` is the full bounding lattice (``nx, ny, nz``); ``active`` stores
    the linear lattice key of every native cell in native cell order. ``None``
    means the grid is dense (native cell position == lattice key), which avoids
    a multi-megabyte map for the common plane/trench cases.
    """

    dimension: int
    delta: float
    origin: tuple[float, float, float]
    shape: tuple[int, int, int]
    active: array | None = field(default=None, repr=False, compare=False)

    @property
    def vertical_axis(self) -> int:
        return VERTICAL_AXIS[int(self.dimension)]

    @property
    def cell_count(self) -> int:
        """Full bounding-lattice cell count (active + inactive)."""
        return self.shape[0] * self.shape[1] * self.shape[2]

    @property
    def active_count(self) -> int:
        """Native cell count (active cells only)."""
        return self.cell_count if self.active is None else len(self.active)

    @property
    def missing_count(self) -> int:
        return self.cell_count - self.active_count

    @property
    def is_sparse(self) -> bool:
        return self.active is not None and self.active_count != self.cell_count

    @property
    def bottom(self) -> float:
        return float(self.origin[self.vertical_axis])

    @property
    def top(self) -> float:
        return float(self.origin[self.vertical_axis] + self.shape[self.vertical_axis] * self.delta)

    @property
    def lateral_axes(self) -> tuple[int, ...]:
        return tuple(axis for axis in range(int(self.dimension)) if axis != self.vertical_axis)

    def index(self, i: int, j: int, k: int) -> int:
        nx, ny, _nz = self.shape
        return i + nx * (j + ny * k)

    def coordinates(self, index: int) -> tuple[int, int, int]:
        nx, ny, _nz = self.shape
        i = index % nx
        rest = index // nx
        j = rest % ny
        k = rest // ny
        return i, j, k

    def active_keys(self) -> Iterable[int]:
        return range(self.cell_count) if self.active is None else self.active

    def with_active(self, keys: Sequence[int] | array) -> "CellGrid":
        keys = keys if isinstance(keys, array) else array("q", (int(key) for key in keys))
        if len(keys) > self.cell_count:
            raise GridRemapError(
                f"active cell map has {len(keys)} keys for a {self.cell_count}-cell bounding lattice")
        return CellGrid(dimension=self.dimension, delta=self.delta, origin=self.origin,
                        shape=self.shape, active=keys)

    def layout_digest(self) -> str:
        """Deterministic digest of the active layout (or a dense marker)."""
        if self.active is None:
            return f"dense:{self.cell_count}"
        return hashlib.sha256(self.active.tobytes()).hexdigest()

    def signature(self) -> dict[str, Any]:
        """Deterministic JSON-friendly description used in the run record."""
        return {
            "dimension": int(self.dimension),
            "gridDelta": float(self.delta),
            "origin": [float(value) for value in self.origin],
            "shape": [int(value) for value in self.shape],
            "fullCellCount": int(self.cell_count),
            "activeCellCount": int(self.active_count),
            "missingCellCount": int(self.missing_count),
            "numberOfCells": int(self.active_count),
            "sparse": bool(self.is_sparse),
            "activeLayoutDigest": self.layout_digest(),
            "bottom": float(self.bottom),
            "top": float(self.top),
        }

    @classmethod
    def from_points(cls, points: Sequence[Sequence[float]], *, dimension: int,
                    delta: float, allow_gaps: bool = False) -> "CellGrid":
        """Build the bounding lattice from native cell-set corner nodes.

        ``allow_gaps`` is used for sparse cell sets: the bounding lattice is
        still derived from the minimum/maximum lattice coordinate even when an
        entire lattice plane has no node (the active map later says which
        cells exist). With ``allow_gaps=False`` a gap is a construction error,
        which keeps the dense unit-test contract strict.
        """
        dimension = int(dimension)
        delta = float(delta)
        if dimension not in VERTICAL_AXIS:
            raise GridRemapError(f"unsupported dimension {dimension!r} for a cell grid")
        if not delta > 0.0:
            raise GridRemapError("cell grid spacing must be positive")
        if not points:
            raise GridRemapError("cell grid has no nodes")
        counts = []
        origin = [0.0, 0.0, 0.0]
        for axis in range(dimension):
            cells = _collapse((point[axis] for point in points), delta)
            if len(cells) < 2:
                raise GridRemapError(f"cell grid has no extent along axis {axis}")
            if not allow_gaps:
                for left, right in zip(cells, cells[1:]):
                    if right - left != 1:
                        raise GridRemapError(
                            "cell grid is not a contiguous regular lattice along axis "
                            f"{axis}: gap of {right - left} cells")
            origin[axis] = cells[0] * delta
            counts.append(cells[-1] - cells[0])
        while len(counts) < 3:
            counts.append(1)
        return cls(dimension=dimension, delta=delta,
                   origin=(origin[0], origin[1], origin[2]),
                   shape=(counts[0], counts[1], counts[2]))


def _axis_offset(old: CellGrid, new: CellGrid, axis: int) -> int:
    """Return ``new_origin - old_origin`` in whole cells along ``axis``."""
    delta = old.delta
    raw = (float(new.origin[axis]) - float(old.origin[axis])) / delta
    whole = round(raw)
    if abs(raw - whole) > 1e-6:
        raise GridRemapError(
            "conservative grid remap not implemented: the regenerated cell grid is not "
            f"aligned to the captured lattice along axis {axis} "
            f"(offset {raw * delta:g} um is not a multiple of gridDelta {delta:g} um)")
    return int(whole)


def _lookup_table(grid: CellGrid) -> array:
    """Sparse ``lattice key -> native position`` table (-1 for inactive)."""
    table = array("i", [-1]) * grid.cell_count
    for position, key in enumerate(grid.active or ()):
        table[int(key)] = position
    return table


def _same_layout(old: CellGrid, new: CellGrid) -> bool:
    if old.shape != new.shape:
        return False
    if any(abs(float(old.origin[axis]) - float(new.origin[axis])) > 1e-9
           for axis in range(3)):
        return False
    if old.active is None and new.active is None:
        return True
    if old.active is None or new.active is None:
        return False
    return old.active == new.active


def _digest(values: Sequence[float]) -> str:
    return hashlib.sha256(array("d", values).tobytes()).hexdigest()


@dataclass
class RemapResult:
    """Remapped scalar arrays plus the audit record for one remap."""

    labels: dict[str, list[float]] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)


def remap_cell_fields(
    old_grid: CellGrid,
    new_grid: CellGrid,
    *,
    old_values: Mapping[str, Sequence[float]],
    old_material: Sequence[float],
    new_material: Sequence[float],
    cover_material_id: int,
    labels: Sequence[str] | None = None,
    cell_volume: float | None = None,
    source_step_id: str | None = None,
    previous_remap_count: int = 0,
) -> RemapResult:
    """Conservatively remap ``old_values`` onto ``new_grid`` by lattice key.

    * identical active layout -> direct index copy (``O(N)``),
    * integer lattice shift/grow/shrink -> key overlap copy, new cells ``0``,
    * inactive new cells (cover/void) simply do not receive data,
    * anything off the lattice -> :class:`GridRemapError` (no interpolation).
    """
    dimension = int(old_grid.dimension)
    if int(new_grid.dimension) != dimension:
        raise GridRemapError("cell grid dimension changed; a field grid cannot change dimension")
    if abs(float(new_grid.delta) - float(old_grid.delta)) > 1e-12:
        raise GridRemapError(
            "conservative grid remap not implemented: the regenerated cell grid has a different "
            f"gridDelta ({new_grid.delta:g} vs {old_grid.delta:g} um)")
    old_active = old_grid.active_count
    new_active = new_grid.active_count
    if len(old_material) != old_active:
        raise GridRemapError(
            f"old cell-set material array length {len(old_material)} does not match the "
            f"{old_active} native cells")
    if len(new_material) != new_active:
        raise GridRemapError(
            f"new cell-set material array length {len(new_material)} does not match the "
            f"{new_active} native cells")
    for label, values in old_values.items():
        if len(values) != old_active:
            raise GridRemapError(
                f"old cell-set scalar {label!r} length {len(values)} does not match the "
                f"{old_active} native cells")

    offsets = [_axis_offset(old_grid, new_grid, axis) for axis in range(dimension)]
    if dimension == 2:
        offsets.append(0)
    layout_same = _same_layout(old_grid, new_grid)

    source = array("i", [-1]) * new_active
    if layout_same:
        for position in range(new_active):
            source[position] = position
    else:
        old_table = _lookup_table(old_grid) if old_grid.active is not None else None
        nx_old, ny_old, nz_old = old_grid.shape
        for position, key in enumerate(new_grid.active_keys()):
            i, j, k = new_grid.coordinates(int(key))
            oi, oj, ok = i + offsets[0], j + offsets[1], k + offsets[2]
            if not (0 <= oi < nx_old and 0 <= oj < ny_old and 0 <= ok < nz_old):
                continue
            old_key = old_grid.index(oi, oj, ok)
            old_position = old_key if old_table is None else int(old_table[old_key])
            if old_position >= 0:
                source[position] = old_position

    matched = 0
    removed_material_indices: list[int] = []
    reactivated = 0
    cover = int(cover_material_id)
    for position in range(new_active):
        old_position = source[position]
        if old_position < 0:
            continue
        matched += 1
        # A new cell is "removed material" when the regenerated grid says it is
        # cover but it used to be a solid cell. Its dopant is dropped, never moved.
        if int(new_material[position]) == cover and int(old_material[old_position]) != cover:
            removed_material_indices.append(position)
        elif int(new_material[position]) != cover and int(old_material[old_position]) == cover:
            reactivated += 1
    removed_set = set(removed_material_indices) if removed_material_indices else None

    label_names = list(labels if labels is not None else old_values.keys())
    dv = float(cell_volume) if cell_volume is not None else float(old_grid.delta) ** dimension
    arrays: dict[str, list[float]] = {}
    for label in label_names:
        old = [float(value) for value in old_values[label]]
        if layout_same and removed_set is None:
            arrays[label] = list(old)
            continue
        values = [old[position] if position >= 0 else 0.0 for position in source]
        if removed_set:
            for position in removed_set:
                values[position] = 0.0
        arrays[label] = values

    masses: dict[str, dict[str, float | None]] = {}
    for label in label_names:
        before = sum(old_values[label]) * dv
        after = sum(arrays[label]) * dv
        lost = before - after
        masses[label] = {
            "before": before,
            "after": after,
            "lost": lost,
            "lostMass": lost,
            "unit": f"concentration*{'um^2' if dimension == 2 else 'um^3'}",
        }

    removed_by_grid = old_active - matched
    new_cells = new_active - matched
    removed_by_material = len(removed_material_indices)
    method = ("same-grid-index-copy" if layout_same
              else "conservative-axis-aligned-integer-overlap")
    record: dict[str, Any] = {
        "sourceStepId": source_step_id,
        "remapIndex": int(previous_remap_count) + 1,
        "method": method,
        "exact": True,
        "interpolation": "none; every new active cell maps to one old active cell or starts at zero",
        "sameGrid": bool(layout_same),
        "sparse": bool(old_grid.is_sparse or new_grid.is_sparse),
        "oldGrid": old_grid.signature(),
        "newGrid": new_grid.signature(),
        "cellOffset": {"x": offsets[0], "y": offsets[1], "z": offsets[2]},
        "oldCellCount": old_active,
        "newCellCount": new_active,
        "oldFullCellCount": old_grid.cell_count,
        "newFullCellCount": new_grid.cell_count,
        "oldMissingCellCount": old_grid.missing_count,
        "newMissingCellCount": new_grid.missing_count,
        "remappedCells": matched,
        "newCells": new_cells,
        "removedCells": removed_by_grid + removed_by_material,
        "removedByGrid": removed_by_grid,
        "removedByMaterial": removed_by_material,
        "reactivatedCells": reactivated,
        "coverMaterialId": cover,
        "cellVolume": dv,
        "labels": list(label_names),
        "mass": masses,
        "fieldDigests": {label: _digest(arrays[label]) for label in label_names},
        "labelsPreserved": True,
        "materialAndFilling": "rebuilt by the native fromLevelSets generator; not remapped",
    }
    return RemapResult(labels=arrays, record=record)


def check_captured_grid(captured: CellGrid, regenerated: CellGrid) -> None:
    """Reject a regeneration that changed the lateral grid or the spacing.

    The vertical extent may grow (an etch removes substrate rows, a deposit
    adds cover rows); the overlap remap handles that conservatively. The
    lateral extent, spacing and origin must stay identical because the domain
    does not change inside a run.
    """
    if int(captured.dimension) != int(regenerated.dimension):
        raise GridRemapError("conservative grid remap not implemented: cell grid dimension changed")
    if abs(captured.delta - regenerated.delta) > 1e-12:
        raise GridRemapError(
            "conservative grid remap not implemented: gridDelta changed "
            f"({captured.delta:g} -> {regenerated.delta:g} um)")
    for axis in captured.lateral_axes:
        if abs(captured.origin[axis] - regenerated.origin[axis]) > 1e-9:
            raise GridRemapError(
                "conservative grid remap not implemented: lateral cell grid origin changed along "
                f"axis {axis} ({captured.origin[axis]:g} -> {regenerated.origin[axis]:g} um)")
        if captured.shape[axis] != regenerated.shape[axis]:
            raise GridRemapError(
                "conservative grid remap not implemented: lateral cell count changed along "
                f"axis {axis} ({captured.shape[axis]} -> {regenerated.shape[axis]})")
