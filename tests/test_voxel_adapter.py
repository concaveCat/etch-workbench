"""Tests for the phase-1 voxel adapter (``process_models.voxel``).

Only native, height-field geometries validated by the probe are used. No
server, no data directory, no project files. The fin case - the only geometry
that is expected to be rejected *and* that can crash natively - runs in its own
subprocess so a native failure is reported as a test failure instead of taking
the test runner down.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np
import viennals as ls
import viennaps as ps

from process_models.voxel import (
    VoxelAdapterError,
    VoxelGrid,
    read_voxels,
    round_trip,
    write_voxels,
)

GRID_DELTA = 0.1
REFLECTIVE = ps.BoundaryType.REFLECTIVE_BOUNDARY
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _configure(dimension: int) -> None:
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.setDimension(dimension)


def _plane_2d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, REFLECTIVE)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _trench_2d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, REFLECTIVE)
    ps.MakeTrench(
        domain=domain,
        trenchWidth=0.5,
        trenchDepth=0.6,
        trenchTaperAngle=0.0,
        maskHeight=0.3,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Mask,
    ).apply()
    return domain


def _thin_film_2d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, REFLECTIVE)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    ps.MakePlane(domain, height=0.2, material=ps.Material.SiO2, addToExisting=True).apply()
    return domain


def _stack_2d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, REFLECTIVE)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    ps.MakePlane(domain, height=0.2, material=ps.Material.SiO2, addToExisting=True).apply()
    ps.MakePlane(domain, height=0.4, material=ps.Material.Mask, addToExisting=True).apply()
    return domain


def _plane_3d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, 1.0, REFLECTIVE)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _trench_3d() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, 1.0, 1.0, REFLECTIVE)
    ps.MakeTrench(
        domain=domain,
        trenchWidth=0.5,
        trenchDepth=0.6,
        trenchTaperAngle=0.0,
        maskHeight=0.3,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Mask,
    ).apply()
    return domain


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _material_order(domain: ps.Domain) -> tuple[int, ...]:
    material_map = domain.getMaterialMap()
    return tuple(int(material_map.getMaterialIdAtIdx(i)) for i in range(material_map.size()))


def _histogram(material: np.ndarray) -> dict[int, int]:
    values, counts = np.unique(material[material >= 0], return_counts=True)
    return {int(value): int(count) for value, count in zip(values, counts)}


def _crop(grid: VoxelGrid, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    origin = np.asarray(grid.origin)
    starts = tuple(int(v) for v in (lo - origin))
    stops = tuple(int(v) for v in (hi - origin))
    return grid.material[tuple(slice(start, stop) for start, stop in zip(starts, stops))]


def _common_region(left: VoxelGrid, right: VoxelGrid) -> tuple[np.ndarray, np.ndarray]:
    """Crop two grids to their shared index window.

    The window of an infinite axis follows the surface extent, which can shift
    by one cell between the original and the reconstructed domain. Comparing
    the shared box avoids attributing that shift to the material layout.
    """
    lo = np.maximum(np.asarray(left.origin), np.asarray(right.origin))
    left_hi = np.asarray(left.origin) + np.asarray(left.material.shape)
    right_hi = np.asarray(right.origin) + np.asarray(right.material.shape)
    hi = np.minimum(left_hi, right_hi)
    if np.any(hi <= lo):
        raise AssertionError(f"grids do not overlap: lo={lo.tolist()} hi={hi.tolist()}")
    return _crop(left, lo, hi), _crop(right, lo, hi)


def _surface_nodes(module, level_set) -> np.ndarray:
    mesh = ls.Mesh()
    module.ToSurfaceMesh(level_set, mesh).apply()
    return np.asarray(mesh.getNodes(), dtype=float).reshape(-1, 3)


def _surface_deviation(module, left, right) -> float:
    """Symmetric nearest-node distance between two level-set surfaces."""
    left_nodes = _surface_nodes(module, left)
    right_nodes = _surface_nodes(module, right)
    distances = np.linalg.norm(left_nodes[:, None, :] - right_nodes[None, :, :], axis=2)
    both = np.concatenate([distances.min(axis=1), distances.min(axis=0)])
    return float(both.max())


def _assert_level_sets_round_trip(
    test: unittest.TestCase,
    domain: ps.Domain,
    *,
    dimension: int,
    levels: int,
    surface_tolerance: float,
) -> ps.Domain:
    module = ls.d3 if dimension == 3 else ls.d2
    grid = read_voxels(domain)
    test.assertEqual(len(grid.material_order), levels)
    test.assertEqual(grid.material_order, _material_order(domain))

    reconstructed = round_trip(domain)
    test.assertEqual(len(reconstructed.getLevelSets()), levels)
    test.assertEqual(_material_order(reconstructed), grid.material_order)

    reread = read_voxels(reconstructed)
    left, right = _common_region(grid, reread)
    test.assertEqual(_histogram(left), _histogram(right))

    for index, (original, rebuilt) in enumerate(zip(domain.getLevelSets(), reconstructed.getLevelSets())):
        deviation = _surface_deviation(module, original, rebuilt)
        test.assertLessEqual(
            deviation,
            surface_tolerance,
            msg=f"level set {index} surface deviation {deviation} > {surface_tolerance}",
        )
    return reconstructed


# ---------------------------------------------------------------------------
# 2D round trips
# ---------------------------------------------------------------------------
class TwoDimensionalRoundTripTest(unittest.TestCase):
    def test_plane_round_trip(self) -> None:
        _configure(2)
        domain = _plane_2d()

        grid = read_voxels(domain)
        self.assertEqual(grid.material.dtype, np.int32)
        self.assertIsInstance(grid.origin, tuple)
        self.assertTrue(all(isinstance(v, int) for v in grid.origin))
        self.assertAlmostEqual(grid.spacing, GRID_DELTA, delta=1e-12)
        self.assertEqual(grid.material_order, _material_order(domain))
        self.assertEqual(grid.fields, {})
        self.assertGreater(int((grid.material >= 0).sum()), 0)
        self.assertTrue(np.all(np.isin(grid.material, [-1, *grid.material_order])))
        # Finite axis follows the grid bounds: x in [-0.5, 0.5] -> 10 cells.
        self.assertEqual(grid.material.shape[0], 10)

        reconstructed = _assert_level_sets_round_trip(self, domain, dimension=2, levels=1, surface_tolerance=1e-9)
        self.assertEqual(_material_order(reconstructed), grid.material_order)
        # Plane round trips are bit exact.
        reread = read_voxels(reconstructed)
        self.assertEqual(reread.origin, grid.origin)
        np.testing.assert_array_equal(reread.material, grid.material)

    def test_trench_round_trip(self) -> None:
        _configure(2)
        domain = _trench_2d()
        grid = read_voxels(domain)
        self.assertEqual(len(grid.material_order), 2)
        histogram = _histogram(grid.material)
        self.assertEqual(set(histogram), set(grid.material_order))

        _assert_level_sets_round_trip(self, domain, dimension=2, levels=2, surface_tolerance=1.5 * GRID_DELTA)

    def test_thin_film_round_trip(self) -> None:
        _configure(2)
        domain = _thin_film_2d()
        grid = read_voxels(domain)
        self.assertEqual(len(grid.material_order), 2)
        histogram = _histogram(grid.material)
        self.assertEqual(set(histogram), set(grid.material_order))
        self.assertEqual(histogram[grid.material_order[1]], 20)  # 2*gd film, 10 cells wide

        reconstructed = _assert_level_sets_round_trip(self, domain, dimension=2, levels=2, surface_tolerance=1e-9)
        reread = read_voxels(reconstructed)
        self.assertEqual(reread.origin, grid.origin)
        np.testing.assert_array_equal(reread.material, grid.material)


# ---------------------------------------------------------------------------
# 3D round trips
# ---------------------------------------------------------------------------
class ThreeDimensionalRoundTripTest(unittest.TestCase):
    def test_plane_round_trip(self) -> None:
        _configure(3)
        domain = _plane_3d()

        grid = read_voxels(domain)
        self.assertEqual(grid.material.dtype, np.int32)
        self.assertEqual(len(grid.origin), 3)
        self.assertEqual(grid.material.shape[:2], (10, 10))  # 1.0 x 1.0 domain
        self.assertEqual(grid.material_order, _material_order(domain))
        self.assertEqual(grid.fields, {})

        reconstructed = _assert_level_sets_round_trip(self, domain, dimension=3, levels=1, surface_tolerance=1e-9)
        reread = read_voxels(reconstructed)
        self.assertEqual(reread.origin, grid.origin)
        np.testing.assert_array_equal(reread.material, grid.material)

    def test_trench_round_trip(self) -> None:
        _configure(3)
        domain = _trench_3d()
        grid = read_voxels(domain)
        self.assertEqual(len(grid.material_order), 2)
        self.assertEqual(grid.material.shape[:2], (10, 10))

        _assert_level_sets_round_trip(self, domain, dimension=3, levels=2, surface_tolerance=1.5 * GRID_DELTA)


# ---------------------------------------------------------------------------
# VoxelGrid validation
# ---------------------------------------------------------------------------
class VoxelGridValidationTest(unittest.TestCase):
    def test_rejects_wrong_dtype(self) -> None:
        with self.assertRaises(VoxelAdapterError):
            VoxelGrid(
                material=np.full((2, 2), -1.0, dtype=np.float64),
                origin=(0, 0),
                spacing=GRID_DELTA,
                material_order=(10,),
            )

    def test_rejects_unknown_material_id(self) -> None:
        material = np.full((2, 2), -1, dtype=np.int32)
        material[0, 0] = 99
        with self.assertRaises(VoxelAdapterError):
            VoxelGrid(
                material=material,
                origin=(0, 0),
                spacing=GRID_DELTA,
                material_order=(10,),
            )

    def test_rejects_non_empty_fields(self) -> None:
        with self.assertRaises(VoxelAdapterError):
            VoxelGrid(
                material=np.full((2, 2), -1, dtype=np.int32),
                origin=(0, 0),
                spacing=GRID_DELTA,
                material_order=(10,),
                fields={"temperature": np.zeros((2, 2), dtype=np.float32)},
            )


# ---------------------------------------------------------------------------
# write_voxels validation and layer removal
# ---------------------------------------------------------------------------
class WriteVoxelsValidationTest(unittest.TestCase):
    def test_rejects_removing_a_middle_layer(self) -> None:
        _configure(2)
        domain = _stack_2d()
        grid = read_voxels(domain)
        self.assertEqual(len(grid.material_order), 3)

        middle = grid.material_order[1]
        material = grid.material.copy()
        material[material == middle] = -1
        broken = VoxelGrid(
            material=material,
            origin=grid.origin,
            spacing=grid.spacing,
            material_order=grid.material_order,
        )
        with self.assertRaises(VoxelAdapterError):
            write_voxels(domain, broken)

    def test_removing_the_outermost_layer_is_allowed(self) -> None:
        _configure(2)
        domain = _stack_2d()
        grid = read_voxels(domain)
        outer = grid.material_order[-1]
        material = grid.material.copy()
        material[material == outer] = -1
        trimmed = VoxelGrid(
            material=material,
            origin=grid.origin,
            spacing=grid.spacing,
            material_order=grid.material_order,
        )

        reconstructed = write_voxels(domain, trimmed)
        self.assertEqual(len(reconstructed.getLevelSets()), 2)
        self.assertEqual(_material_order(reconstructed), grid.material_order[:2])
        # The input domain is untouched.
        self.assertEqual(len(domain.getLevelSets()), 3)

    def test_rejects_unknown_material_ids(self) -> None:
        _configure(2)
        domain = _plane_2d()
        grid = read_voxels(domain)
        # A plugin may edit the array in place; that bypasses VoxelGrid
        # validation, so write_voxels has to reject unknown IDs itself.
        unknown = max(grid.material_order) + 77
        grid.material[grid.material >= 0] = unknown
        with self.assertRaises(VoxelAdapterError):
            write_voxels(domain, grid)

    def test_rejects_empty_material_grid(self) -> None:
        _configure(2)
        domain = _plane_2d()
        grid = read_voxels(domain)
        empty = VoxelGrid(
            material=np.full(grid.material.shape, -1, dtype=np.int32),
            origin=grid.origin,
            spacing=grid.spacing,
            material_order=grid.material_order,
        )
        with self.assertRaises(VoxelAdapterError):
            write_voxels(domain, empty)

    def test_rejects_mismatched_spacing(self) -> None:
        _configure(2)
        domain = _plane_2d()
        grid = read_voxels(domain)
        wrong = VoxelGrid(
            material=grid.material,
            origin=grid.origin,
            spacing=GRID_DELTA * 2.0,
            material_order=grid.material_order,
        )
        with self.assertRaises(VoxelAdapterError):
            write_voxels(domain, wrong)


# ---------------------------------------------------------------------------
# determinism and the height-field guard
# ---------------------------------------------------------------------------
class DeterminismTest(unittest.TestCase):
    def test_reading_twice_is_identical(self) -> None:
        _configure(2)
        domain = _trench_2d()
        first = read_voxels(domain)
        second = read_voxels(domain)
        self.assertEqual(first.origin, second.origin)
        self.assertEqual(first.material_order, second.material_order)
        np.testing.assert_array_equal(first.material, second.material)


_FIN_GUARD_SCRIPT = """
import viennaps as ps

from process_models.voxel import VoxelAdapterError, read_voxels

ps.setNumThreads(1)
ps.Length.setUnit("um")
ps.setDimension(2)
domain = ps.Domain(0.1, 1.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
ps.MakeFin(
    domain,
    finWidth=0.4,
    finHeight=0.3,
    finTaperAngle=0.0,
    maskHeight=0.2,
    maskTaperAngle=0.0,
    halfFin=False,
    material=ps.Material.Si,
    maskMaterial=ps.Material.Mask,
).apply()

try:
    read_voxels(domain, window_cells=2)
except VoxelAdapterError:
    raise SystemExit(0)
raise SystemExit(3)
"""


class HeightFieldGuardTest(unittest.TestCase):
    def test_fin_is_rejected_in_a_subprocess(self) -> None:
        # The fin case is the geometry where ToVoxelMesh/Expand misbehave; run
        # it out of process so a native crash is a test failure, not a dead
        # test runner.
        completed = subprocess.run(
            [sys.executable, "-c", _FIN_GUARD_SCRIPT],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=(
                "fin guard subprocess failed with return code "
                f"{completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
