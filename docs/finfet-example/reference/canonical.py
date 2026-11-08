"""Canonical parity digests shared by the FinFET reference runner and tests.

The native surface/volume writers and the workbench snapshot store the same
mesh in different orders. A digest must therefore be invariant to point order,
cell order, and the rotation of a cell's vertex list, while still covering the
exact geometry and material association.

All values are rounded to nine decimals before hashing. The reference uses
nanometre values scaled to micrometres, and both paths carry the same binary
float values, so nine decimals is well above the native grid precision and far
below any meaningful geometric difference.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

import numpy as np

__all__ = ["surface_digest", "volume_digest", "digest_arrays"]

_DECIMALS = 9


def _array(values: Sequence[Any]) -> np.ndarray:
    return np.round(np.asarray(values, dtype=np.float64), _DECIMALS)


def _canonical(points: Any, cells: Any, material_ids: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return points and cells in a deterministic order with material IDs.

    Duplicate coordinates keep a stable identity by breaking ties with the
    original point index. Cells are remapped through that permutation, each
    cell's vertex list is sorted (a cell is a set for parity purposes), and the
    cell rows are lexicographically sorted together with their material ID.
    """
    point_array = _array(points)
    cell_array = np.asarray(cells, dtype=np.int64)
    materials = np.asarray(material_ids, dtype=np.int64)
    if point_array.ndim != 2 or point_array.shape[1] != 3:
        raise ValueError(f"points must be Nx3, got {point_array.shape}")
    if cell_array.ndim != 2 or cell_array.shape[0] != materials.shape[0]:
        raise ValueError("cells and material IDs are not aligned")
    if cell_array.size and (cell_array.min() < 0 or cell_array.max() >= len(point_array)):
        raise ValueError("cell connectivity is outside the point array")
    if materials.size and materials.min() < 0:
        raise ValueError("material IDs must be non-negative")

    index = np.arange(len(point_array), dtype=np.int64)
    order = np.lexsort((index, point_array[:, 2], point_array[:, 1], point_array[:, 0]))
    rank = np.empty(len(point_array), dtype=np.int64)
    rank[order] = np.arange(len(point_array), dtype=np.int64)
    remapped = rank[cell_array]
    # Sorting each row makes the digest independent of vertex rotation/order.
    remapped.sort(axis=1)
    if len(remapped):
        keys = tuple(remapped[:, column] for column in range(remapped.shape[1] - 1, -1, -1))
        cell_order = np.lexsort((materials,) + keys)
        remapped = remapped[cell_order]
        materials = materials[cell_order]
    return point_array[order], remapped, materials


def digest_arrays(points: Any, cells: Any, material_ids: Any, *, kind: str) -> str:
    """Hash one canonical mesh and return its SHA-256 hex digest."""
    canonical_points, canonical_cells, materials = _canonical(points, cells, material_ids)
    digest = hashlib.sha256()
    digest.update(b"viennaps-workbench-mesh-v1\0")
    digest.update(str(kind).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(len(canonical_points)).encode("ascii"))
    digest.update(b"\0")
    digest.update(str(len(canonical_cells)).encode("ascii"))
    digest.update(b"\0")
    digest.update(np.ascontiguousarray(canonical_points, dtype="<f8").tobytes())
    digest.update(np.ascontiguousarray(canonical_cells, dtype="<i8").tobytes())
    digest.update(np.ascontiguousarray(materials, dtype="<i8").tobytes())
    return digest.hexdigest()


def surface_digest(points: Any, triangles: Any, material_ids: Any) -> str:
    """Canonical digest of a triangle surface mesh."""
    return digest_arrays(points, triangles, material_ids, kind="surface")


def volume_digest(nodes: Any, cells: Any, material_ids: Any) -> str:
    """Canonical digest of a tetrahedral volume mesh."""
    return digest_arrays(nodes, cells, material_ids, kind="volume")
