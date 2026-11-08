"""Voxel adapter between ViennaPS level-set domains and dense index grids.

The adapter is deliberately narrow: it supports **height-field** geometries
(plane, layer stack, trench, hole with a wafer-covering mask) whose level sets
span the lateral domain. Fin-like shapes and GDS cut-outs are rejected because
``viennals.ToVoxelMesh`` reads undefined memory outside such level-set bands and
``viennals.Expand`` creates spurious surfaces for re-entrant shapes.

Representation
--------------
A :class:`VoxelGrid` is a dense array in *global cell index space*:

* ``material[i, j]`` (2D) or ``material[i, j, k]`` (3D) holds a native
  viennaps material ID, or ``-1`` for void.
* Cell ``(i, j)`` spans ``[i*s, (i+1)*s] x ...`` with ``s = spacing``; its
  centre is ``(i + 0.5) * s`` and its physical coordinate is
  ``(origin + index) * spacing``.
* ``material_order`` is the native material ID of each level set in
  ``domain.getLevelSets()`` order (innermost first).

``viennals.ToVoxelMesh`` labels cells with the *level-set insertion index*
``0..N-1`` and does not carry native material IDs, so the adapter keeps the
index <-> material mapping itself and re-inserts the original material handles
on write-back.

Write-back
----------
Material IDs do not survive a native round trip, and ``FromVolumeMesh`` is
unusable from Python (no ``getLevelSets``, 2D element mismatch). The adapter
therefore extracts the boundary faces of each cumulative cell region and feeds
them to ``viennals.FromSurfaceMesh`` with outward normals and
``setRemoveBoundaryTriangles(False)``. Faces on the array border are left open:
the array is a clip window, not a wall.

Native safety
-------------
Native calls in this area have been observed to segfault (delayed
use-after-free, undefined dense-iterator reads). The adapter keeps every
Python-owned level set alive until the converter has run and refuses geometries
that are known to be unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import viennals as ls
import viennaps as ps

__all__ = ["VoxelAdapterError", "VoxelGrid", "read_voxels", "round_trip", "write_voxels"]

#: Boundary conditions whose axis has no reflective wall. The grid bounds are
#: not meaningful there, so the window is derived from the surface extent.
_INFINITE_BOUNDARIES = frozenset(
    {
        int(ps.BoundaryType.INFINITE_BOUNDARY),
        int(ps.BoundaryType.NEG_INFINITE_BOUNDARY),
        int(ps.BoundaryType.POS_INFINITE_BOUNDARY),
    }
)

#: Height-field tolerance: a material cell centre higher than this many grid
#: spacings above the highest original surface means the geometry is not a
#: height field (spurious ``ToVoxelMesh`` cells, e.g. fin-like shapes).
_HEIGHT_SLACK_CELLS = 2.0


class VoxelAdapterError(RuntimeError):
    """Raised when a domain or voxel grid cannot be converted safely."""


@dataclass(frozen=True)
class VoxelGrid:
    """Dense material grid in global cell-index space.

    Attributes:
        material: ``int32`` array; ``-1`` is void, otherwise a native
            viennaps material ID.
        origin: global cell-index origin (corner based), one entry per
            dimension.
        spacing: grid delta in physical units.
        material_order: native material IDs in level-set insertion order.
        fields: reserved for step-local scalar fields; must stay empty in
            phase 1.
    """

    material: np.ndarray
    origin: tuple[int, ...]
    spacing: float
    material_order: tuple[int, ...]
    fields: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        material = np.asarray(self.material)
        if material.ndim not in (2, 3):
            raise VoxelAdapterError(f"material must be 2D or 3D, got {material.ndim}D")
        if not np.issubdtype(material.dtype, np.integer):
            raise VoxelAdapterError(f"material dtype must be an integer type, got {material.dtype}")

        origin = tuple(int(v) for v in self.origin)
        if len(origin) != material.ndim:
            raise VoxelAdapterError(f"origin has {len(origin)} entries but material is {material.ndim}D")
        if any(int(size) < 0 for size in material.shape):
            raise VoxelAdapterError(f"material shape must be non-negative, got {material.shape}")

        spacing = float(self.spacing)
        if not np.isfinite(spacing) or spacing <= 0.0:
            raise VoxelAdapterError(f"spacing must be a positive finite number, got {spacing}")

        order = tuple(int(v) for v in self.material_order)
        if any(mid < 0 for mid in order):
            raise VoxelAdapterError("material_order must contain native material IDs >= 0")

        material = material.astype(np.int32, copy=False)
        allowed = set(order)
        for mid in np.unique(material):
            value = int(mid)
            if value != -1 and value not in allowed:
                raise VoxelAdapterError(f"material id {value} is neither -1 (void) nor in material_order {order}")

        if not isinstance(self.fields, dict):
            raise VoxelAdapterError("fields must be a dict")
        if self.fields:
            raise VoxelAdapterError("scalar fields are not supported in phase 1; fields must be empty")

        object.__setattr__(self, "material", material)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "spacing", spacing)
        object.__setattr__(self, "material_order", order)


def read_voxels(domain: Any, *, window_cells: int = 2) -> VoxelGrid:
    """Voxelize a height-field level-set domain.

    Args:
        domain: A ``viennaps.Domain`` (2D or 3D).
        window_cells: Number of void cells kept beyond the surface on axes with
            an infinite boundary condition.

    Returns:
        A :class:`VoxelGrid` covering the finite domain bounds on reflective or
        periodic axes and the surface extent plus ``window_cells`` on infinite
        axes.

    Raises:
        VoxelAdapterError: If the domain has no level sets, the material map is
            inconsistent, ``ToVoxelMesh`` emits unknown labels, or the geometry
            is not a height field.
    """
    if int(window_cells) < 0:
        raise VoxelAdapterError(f"window_cells must be non-negative, got {window_cells}")
    window_cells = int(window_cells)

    dim = _domain_dimension(domain)
    spacing = float(domain.getGridDelta())
    if not np.isfinite(spacing) or spacing <= 0.0:
        raise VoxelAdapterError(f"domain grid delta must be positive, got {spacing}")

    level_sets = list(domain.getLevelSets())
    if not level_sets:
        raise VoxelAdapterError("domain has no level sets to voxelize")
    material_order = _material_order(domain)

    module = ls.d3 if dim == 3 else ls.d2
    surf_lo, surf_hi = _surface_extent(module, level_sets, dim)
    lo, hi = _adapter_window(domain.getSetup(), surf_lo, surf_hi, spacing, window_cells, dim)
    shape = tuple(int(v) for v in (hi - lo + 1))

    # Expand wide enough that the narrow band covers the whole window: outside
    # the band ToVoxelMesh reads undefined dense-iterator values.
    surface_span_cells = int(np.abs(surf_hi - surf_lo).max() / spacing) + 2 if level_sets else 0
    window_span_cells = int(np.max(hi - lo)) + 1
    expand_width = 2 * (window_cells + max(surface_span_cells, window_span_cells) + 2)

    mesh = ls.Mesh()
    converter = module.ToVoxelMesh(mesh)
    expanded: list[Any] = []
    try:
        for level_set in level_sets:
            # ToVoxelMesh keeps C++ references to the level sets; the Python
            # owners must outlive apply().
            copy = module.Domain(level_set)
            ls.Expand(copy, expand_width).apply()
            expanded.append(copy)
            converter.insertNextLevelSet(copy)
        converter.apply()

        nodes = np.asarray(mesh.getNodes(), dtype=float)
        elements = np.asarray(mesh.getHexas() if dim == 3 else mesh.getTetras(), dtype=np.int64)
        labels = np.asarray(mesh.getCellData().getScalarData(0), dtype=np.int64)
        if elements.size and elements.shape[0] != labels.size:
            raise VoxelAdapterError(f"voxel mesh has {elements.shape[0]} cells but {labels.size} material labels")

        material = np.full(shape, -1, dtype=np.int32)
        if elements.size:
            corners = _cell_corners(nodes, elements, spacing, dim)
            if labels.size and (int(labels.min()) < 0 or int(labels.max()) >= len(material_order)):
                raise VoxelAdapterError(
                    f"ToVoxelMesh emitted a level-set label outside the material order 0..{len(material_order) - 1}"
                )
            _check_height_field(corners, labels, surf_hi[-1], spacing)
            _fill_window(material, corners, labels, lo, hi, material_order)
    finally:
        # Drop the converter before its level sets to avoid a delayed
        # use-after-free SIGSEGV.
        del converter
        del expanded

    return VoxelGrid(
        material=material,
        origin=tuple(int(v) for v in lo),
        spacing=spacing,
        material_order=material_order,
    )


def write_voxels(domain: Any, grid: VoxelGrid) -> Any:
    """Rebuild a new level-set domain from a :class:`VoxelGrid`.

    The input domain is not mutated. Only the present *outermost* level-set
    layers may be removed: the present level-set indices must form a prefix
    ``0..k`` of ``grid.material_order``.

    Args:
        domain: The reference ``viennaps.Domain`` (grid, bounds, materials).
        grid: Voxel data produced by :func:`read_voxels` (possibly edited).

    Returns:
        A new ``viennaps.Domain`` whose level sets are negative inside the
        cumulative material regions ``0..k``.

    Raises:
        VoxelAdapterError: If the grid does not match the domain, contains
            unknown material IDs, removes a non-outermost layer, or has no
            material cells left.
    """
    dim = _domain_dimension(domain)
    spacing = float(domain.getGridDelta())
    if abs(float(grid.spacing) - spacing) > 1e-12:
        raise VoxelAdapterError(f"grid spacing {grid.spacing!r} does not match domain grid delta {spacing!r}")

    material = np.asarray(grid.material)
    if material.ndim != dim:
        raise VoxelAdapterError(f"material is {material.ndim}D but the domain is {dim}D")
    if len(grid.origin) != dim:
        raise VoxelAdapterError(f"origin has {len(grid.origin)} entries but the domain is {dim}D")

    order = tuple(int(v) for v in grid.material_order)
    domain_order = _material_order(domain)
    if order != domain_order:
        raise VoxelAdapterError(f"grid material_order {order} does not match the domain material order {domain_order}")

    rank_of = {mid: index for index, mid in enumerate(order)}
    for mid in np.unique(material):
        value = int(mid)
        if value == -1:
            continue
        if value not in rank_of:
            raise VoxelAdapterError(f"material id {value} is not in material_order {order}")

    present = sorted(rank_of[int(mid)] for mid in np.unique(material) if int(mid) >= 0)
    if not present:
        raise VoxelAdapterError("voxel grid contains no material cells")
    if present != list(range(present[-1] + 1)):
        raise VoxelAdapterError(
            "only outermost level-set layers may be removed; "
            f"present level-set indices {present} are not a prefix of 0..{len(order) - 1}"
        )

    ranks = np.full(material.shape, -1, dtype=np.int32)
    for index, mid in enumerate(order):
        ranks[material == mid] = index

    module = ls.d3 if dim == 3 else ls.d2
    level_sets = []
    for index in range(present[-1] + 1):
        region = (ranks >= 0) & (ranks <= index)
        mesh = _region_surface_mesh(region, tuple(int(v) for v in grid.origin), spacing, dim)
        level_set = module.Domain(domain.getGrid())
        converter = module.FromSurfaceMesh(level_set, mesh)
        converter.setRemoveBoundaryTriangles(False)
        converter.apply()
        del converter
        del mesh
        level_sets.append(level_set)

    # Native material IDs are not carried by the level sets: re-insert the
    # original handles in their original order.
    handles = [domain.getMaterialMap().getMaterialAtIdx(i) for i in range(len(order))]

    new_domain = ps.Domain(domain)
    new_domain.clear()
    for index, level_set in enumerate(level_sets):
        new_domain.insertNextLevelSetAsMaterial(level_set, handles[index], True)
    return new_domain


def round_trip(domain: Any, *, window_cells: int = 2) -> Any:
    """Read a domain into voxels and write it back into a new domain."""
    return write_voxels(domain, read_voxels(domain, window_cells=window_cells))


# ---------------------------------------------------------------------------
# domain introspection
# ---------------------------------------------------------------------------
def _domain_dimension(domain: Any) -> int:
    dim = len(domain.getSetup().boundaryCons())
    if dim not in (2, 3):
        raise VoxelAdapterError(f"unsupported domain dimension {dim}")
    return dim


def _material_order(domain: Any) -> tuple[int, ...]:
    material_map = domain.getMaterialMap()
    size = int(material_map.size())
    n_level_sets = len(domain.getLevelSets())
    if size != n_level_sets:
        raise VoxelAdapterError(f"material map size {size} does not match the number of level sets {n_level_sets}")
    if size == 0:
        raise VoxelAdapterError("domain has no materials")
    order = tuple(int(material_map.getMaterialIdAtIdx(i)) for i in range(size))
    if any(mid < 0 for mid in order):
        raise VoxelAdapterError(f"material map contains an undefined material id: {order}")
    return order


def _surface_extent(module: Any, level_sets: list[Any], dim: int) -> tuple[np.ndarray, np.ndarray]:
    surf_lo = np.full(dim, np.inf)
    surf_hi = np.full(dim, -np.inf)
    for level_set in level_sets:
        mesh = ls.Mesh()
        module.ToSurfaceMesh(level_set, mesh).apply()
        nodes = np.asarray(mesh.getNodes(), dtype=float)
        if nodes.size == 0:
            continue
        points = nodes.reshape(-1, 3)[:, :dim]
        surf_lo = np.minimum(surf_lo, points.min(axis=0))
        surf_hi = np.maximum(surf_hi, points.max(axis=0))
    return surf_lo, surf_hi


def _adapter_window(
    setup: Any,
    surf_lo: np.ndarray,
    surf_hi: np.ndarray,
    spacing: float,
    window_cells: int,
    dim: int,
) -> tuple[np.ndarray, np.ndarray]:
    bounds = list(setup.bounds())
    boundaries = [int(bc) for bc in setup.boundaryCons()]
    lo = np.zeros(dim, dtype=np.int64)
    hi = np.zeros(dim, dtype=np.int64)
    for axis in range(dim):
        if boundaries[axis] in _INFINITE_BOUNDARIES:
            if np.isfinite(surf_lo[axis]) and np.isfinite(surf_hi[axis]):
                lo[axis] = int(np.floor(surf_lo[axis] / spacing)) - window_cells
                hi[axis] = int(np.ceil(surf_hi[axis] / spacing)) + window_cells
            else:
                lo[axis] = -window_cells
                hi[axis] = window_cells
        else:
            lo[axis] = int(round(bounds[2 * axis] / spacing))
            hi[axis] = int(round(bounds[2 * axis + 1] / spacing)) - 1
    if np.any(hi < lo):
        raise VoxelAdapterError(f"adapter window is empty: origin {lo.tolist()}, upper corner {hi.tolist()}")
    return lo, hi


# ---------------------------------------------------------------------------
# mesh <-> cells
# ---------------------------------------------------------------------------
def _cell_corners(nodes: np.ndarray, elements: np.ndarray, spacing: float, dim: int) -> np.ndarray:
    if nodes.size == 0:
        raise VoxelAdapterError("voxel mesh has cells but no nodes")
    points = nodes.reshape(-1, 3)[:, :dim]
    # Cells are emitted on the global grid, so node positions are index*spacing.
    return np.round(points[elements].min(axis=1) / spacing).astype(np.int64)


def _check_height_field(
    corners: np.ndarray,
    labels: np.ndarray,
    max_surface_height: float,
    spacing: float,
) -> None:
    if labels.size == 0 or not np.isfinite(max_surface_height):
        return
    material_cells = labels >= 0
    if not material_cells.any():
        return
    centers = (corners[material_cells, -1] + 0.5) * spacing
    if float(centers.max()) > max_surface_height + _HEIGHT_SLACK_CELLS * spacing:
        raise VoxelAdapterError("voxel adapter supports height-field surfaces only; fin/GDS cut-outs are not supported")


def _fill_window(
    material: np.ndarray,
    corners: np.ndarray,
    labels: np.ndarray,
    lo: np.ndarray,
    hi: np.ndarray,
    material_order: tuple[int, ...],
) -> None:
    relative = corners - lo
    inside = np.all((relative >= 0) & (relative <= (hi - lo)), axis=1)
    relative = relative[inside]
    inside_labels = labels[inside]
    if relative.size == 0:
        return
    lookup = np.asarray(material_order, dtype=np.int32)
    material[tuple(relative.T)] = lookup[inside_labels]


def _region_surface_mesh(
    region: np.ndarray,
    origin: tuple[int, ...],
    spacing: float,
    dim: int,
) -> Any:
    """Boundary faces of a boolean cell region, normals pointing outward.

    Faces on the array border are omitted (open clip surface, not a wall).
    """
    mesh = ls.Mesh()
    node_ids: dict[tuple[int, ...], int] = {}

    def node_id(index: tuple[int, ...]) -> int:
        key = tuple(int(v) for v in index)
        if key not in node_ids:
            world = [(key[i] + origin[i]) * spacing for i in range(dim)]
            node_ids[key] = mesh.insertNextNode([world[0], world[1], world[2] if dim == 3 else 0.0])
        return node_ids[key]

    shape = region.shape
    for cell in np.argwhere(region):
        cell = tuple(int(v) for v in cell)
        for axis in range(dim):
            for sign in (-1, 1):
                neighbour = list(cell)
                neighbour[axis] += sign
                if not (0 <= neighbour[axis] < shape[axis]):
                    continue  # border face stays open
                if region[tuple(neighbour)]:
                    continue
                corners = _face_corners(cell, axis, sign, dim)
                ids = [node_id(corner) for corner in corners]
                if dim == 2:
                    points = np.asarray(
                        [[(corner[i] + origin[i]) * spacing for i in range(dim)] for corner in corners],
                        dtype=float,
                    )
                    delta = points[1] - points[0]
                    outward = np.zeros(2)
                    outward[axis] = sign
                    if float(np.dot([-delta[1], delta[0]], outward)) < 0.0:
                        ids.reverse()
                    mesh.insertNextLine(ids)
                else:
                    points = np.asarray(
                        [[(corner[i] + origin[i]) * spacing for i in range(dim)] for corner in corners],
                        dtype=float,
                    )
                    outward = np.zeros(3)
                    outward[axis] = sign
                    for triangle in ((0, 1, 2), (0, 2, 3)):
                        vertices = [ids[i] for i in triangle]
                        v0, v1, v2 = points[list(triangle)]
                        normal = np.cross(v1 - v0, v2 - v0)
                        if float(np.dot(normal, outward)) < 0.0:
                            vertices = [vertices[0], vertices[2], vertices[1]]
                        mesh.insertNextTriangle(vertices)
    return mesh


def _face_corners(cell: tuple[int, ...], axis: int, sign: int, dim: int) -> list[tuple[int, ...]]:
    """Corner index tuples of one cell face (2 nodes in 2D, 4 in 3D)."""
    base = list(cell)
    base[axis] += 1 if sign > 0 else 0
    others = [a for a in range(dim) if a != axis]
    if dim == 2:
        other = others[0]
        first = list(base)
        second = list(base)
        second[other] += 1
        return [tuple(first), tuple(second)]

    def at(u: int, v: int) -> tuple[int, ...]:
        point = list(base)
        point[others[0]] += u
        point[others[1]] += v
        return tuple(point)

    return [at(0, 0), at(1, 0), at(1, 1), at(0, 1)]
