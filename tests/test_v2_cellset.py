"""Native DenseCellSet <-> CellSetState round-trip tests.

These tests use a real ViennaPS domain but a small grid. They cover the
conversion that field steps pay only at sequence boundaries: dense grid
detection, x-fastest native order, int32 material IDs, field arrays, and the
fail-closed geometry digest.
"""

from __future__ import annotations

import shutil
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import viennaps as ps

from process_models.v2.cellset import CellSetBackendError, CellSetConfig
from process_models.v2.contract import ProcessContext
from process_models.v2.engines.viennaps_cellset import ViennaPSCellSetBackend
from process_models.v2.state import LevelSetState

ROOT = Path(__file__).resolve().parents[1]
GRID_DELTA = 0.02
X_EXTENT = 0.4
HEIGHT = 0.2
SUBSTRATE = 0.1


def _plane(*, height: float = 0.0) -> tuple[object, LevelSetState]:
    ps.setDimension(2)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=height, material=ps.Material.Si, addToExisting=False).apply()
    return domain, LevelSetState(handle=domain, dimension=2, grid_delta=GRID_DELTA)


def _plane3d() -> tuple[object, LevelSetState]:
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        GRID_DELTA,
        X_EXTENT,
        X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain, LevelSetState(handle=domain, dimension=3, grid_delta=GRID_DELTA)


def _context(step_id: str = "implant_step_01") -> ProcessContext:
    return ProcessContext(
        dimension=2,
        grid_delta=GRID_DELTA,
        project={"dimension": 2, "domain": {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT}},
        step={
            "id": step_id,
            "model": "implant",
            "cellSet": {
                "height": HEIGHT,
                "substrateDepth": SUBSTRATE,
                "coverMaterial": "GAS",
                "periodic": False,
            },
        },
    )


class ViennaPSCellSetBackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work_dir = ROOT / "var" / f"test-v2-cellset-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.backend = ViennaPSCellSetBackend(ps)
        self.config = CellSetConfig(height=HEIGHT, substrate_depth=SUBSTRATE)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_snapshot_uses_native_x_fastest_order(self) -> None:
        _domain, geometry = _plane()
        state = self.backend.ensure(geometry, _context(), self.config, source_step_id="implant_step_01")

        self.assertEqual(state.grid.array_order, "F")
        self.assertEqual(len(state.grid.shape), 2)
        self.assertEqual(int(np.prod(state.grid.shape)), state.handle.getNumberOfCells())
        self.assertEqual(state.provenance["sourceStepId"], "implant_step_01")
        self.assertEqual(state.provenance["height"], HEIGHT)
        self.assertEqual(state.provenance["substrateDepth"], SUBSTRATE)
        self.assertIn("FillingFraction", state.fields)
        self.assertNotIn("Material", state.fields)
        self.assertEqual(state.material_ids.dtype, np.int32)

        # Native cell 0 and cell 1 differ by one x cell, not one y cell.
        first = state.handle.getCellCenter(0)
        second = state.handle.getCellCenter(1)
        index0 = (
            round((first[0] - state.grid.origin[0]) / GRID_DELTA - 0.5),
            round((first[1] - state.grid.origin[1]) / GRID_DELTA - 0.5),
        )
        index1 = (
            round((second[0] - state.grid.origin[0]) / GRID_DELTA - 0.5),
            round((second[1] - state.grid.origin[1]) / GRID_DELTA - 0.5),
        )
        self.assertEqual(index0, (0, 0))
        self.assertEqual(index1, (1, 0))

    def test_round_trip_preserves_material_and_fields(self) -> None:
        _domain, geometry = _plane()
        state = self.backend.ensure(geometry, _context(), self.config)
        native = state.handle
        cells = int(native.getNumberOfCells())
        native.addScalarData("P_total", 0.0)
        values = np.linspace(1.0, float(cells), num=cells, dtype=np.float64)
        native.setScalarData("P_total", values.tolist())

        snapshot = self.backend.snapshot(native, _context("anneal_step_01"), base=state)
        self.assertIn("P_total", snapshot.fields)
        np.testing.assert_array_equal(snapshot.fields["P_total"].data.reshape(-1, order="F"), values)
        np.testing.assert_array_equal(
            snapshot.material_ids,
            state.material_ids,
        )

        stored = replace(snapshot, handle=None, domain_handle=None)
        self.assertIsNone(stored.handle)
        restored = self.backend.rehydrate(geometry, stored, _context(), self.config)
        self.assertIsNotNone(restored.handle)
        self.assertEqual(restored.handle.getNumberOfCells(), cells)
        np.testing.assert_array_equal(restored.handle.getScalarData("P_total"), values)
        np.testing.assert_array_equal(restored.handle.getScalarData("Material"), native.getScalarData("Material"))

    def test_round_trip_three_dimensional_grid(self) -> None:
        _domain, geometry = _plane3d()
        state = self.backend.ensure(
            geometry,
            ProcessContext(
                dimension=3,
                grid_delta=GRID_DELTA,
                project={"dimension": 3, "domain": {"gridDelta": GRID_DELTA}},
                step={"id": "implant_step_01", "model": "implant"},
            ),
            self.config,
        )
        self.assertEqual(state.grid.dimension, 3)
        self.assertEqual(state.grid.array_order, "F")
        self.assertEqual(len(state.grid.shape), 3)
        self.assertEqual(int(np.prod(state.grid.shape)), state.handle.getNumberOfCells())
        stored = replace(state, handle=None, domain_handle=None)
        restored = self.backend.rehydrate(
            geometry,
            stored,
            ProcessContext(
                dimension=3,
                grid_delta=GRID_DELTA,
                project={"dimension": 3, "domain": {"gridDelta": GRID_DELTA}},
                step={"id": "implant_step_01", "model": "implant"},
            ),
            self.config,
        )
        np.testing.assert_array_equal(restored.material_ids, state.material_ids)

    def test_digest_is_stable_across_a_domain_copy(self) -> None:
        domain, geometry = _plane()
        copied = ps.Domain(domain)
        self.assertEqual(
            self.backend.geometry_digest(geometry),
            self.backend.geometry_digest(LevelSetState(handle=copied, dimension=2, grid_delta=GRID_DELTA)),
        )

    def test_geometry_change_is_refused_instead_of_remapped(self) -> None:
        _domain, geometry = _plane()
        state = self.backend.ensure(geometry, _context(), self.config)
        _other_domain, changed = _plane(height=0.04)
        stored = replace(state, handle=None, domain_handle=None)
        with self.assertRaises(CellSetBackendError):
            self.backend.rehydrate(changed, stored, _context(), self.config)


if __name__ == "__main__":
    unittest.main()
