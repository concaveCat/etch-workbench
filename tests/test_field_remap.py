"""Cell-set regeneration / conservative remap for the 4.7.0 field steps.

The native ``DenseCellSet`` is generated from the level sets. A surface-moving
step between two field steps therefore invalidates it. The worker regenerates a
native cell set on the captured grid and remaps every scalar field by absolute
lattice position (no interpolation, no silent redistribution).

This module pins

* the pure-Python mapping (same-grid copy, integer-shift overlap, mass
  accounting, refusals) without needing a native runtime;
* real 2D/3D ``implant → etch → anneal → analysis`` runs that record the remap;
* a deposition that keeps the grid and the dopant mass;
* a deep etch that has to grow the grid downwards (conservative overlap);
* replay determinism of the remap record and the field digests.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from array import array
from importlib.metadata import version
from itertools import product
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

from backend.field_remap import (CellGrid, GridRemapError,  # noqa: E402
                                 check_captured_grid, remap_cell_fields)
from backend.main import app  # noqa: E402
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_implant import (anneal_step, analysis_step,  # noqa: E402
                                implant_step, project)
from backend import storage  # noqa: E402
import backend.snapshots as snapshot_store  # noqa: E402


def field_settings(**overrides) -> dict:
    value = {"height": 0.5, "substrateDepth": 0.4, "coverMaterial": "GAS",
             "periodic": False, "format": "json"}
    value.update(overrides)
    return value


def boolean_step(**overrides) -> dict:
    value = {"id": "boolean_step_01", "name": "grow", "enabled": True,
             "model": "boolean", "booleanOperation": "union",
             "position": [0.0, -0.1, 0.0], "width": 4.0, "height": 2.1,
             "applyToAll": True}
    value.update(overrides)
    return value


def planarize_step(**overrides) -> dict:
    value = {"id": "planarize_step_01", "name": "planarize", "enabled": True,
             "model": "planarize", "cutoffHeight": 0.0}
    value.update(overrides)
    return value


def fin_geometry(dimension: int) -> dict:
    """A fin whose mask band protrudes above the position plane.

    Measured on 4.7.0: ``finHeight=0.3`` with ``maskHeight=0.2`` produces a
    native ``DenseCellSet`` whose bounding lattice is 20x33 (2D) / 12x12x33
    (3D) while only 517 / 3794 cells are active, the sparse layout from the
    GDS FinFET flow.
    """
    return {"kind": "fin", "width": 0.4 if dimension == 2 else 0.2,
            "height": 0.3, "taperAngle": 0.0, "maskHeight": 0.2,
            "maskTaperAngle": 0.0, "substrateMaterial": "Si",
            "maskMaterial": "Mask"}


def make_grid(*, dimension: int = 2, delta: float = 0.5,
              origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
              shape: tuple[int, int, int] = (4, 4, 1)) -> CellGrid:
    return CellGrid(dimension=dimension, delta=delta, origin=origin, shape=shape)


def grid_nodes(grid: CellGrid) -> list[list[float]]:
    axes = range(int(grid.dimension))
    ranges = [range(grid.shape[axis] + 1) for axis in axes]
    points = []
    for index in product(*ranges):
        point = [0.0, 0.0, 0.0]
        for axis in axes:
            point[axis] = grid.origin[axis] + index[axis] * grid.delta
        points.append(point)
    return points


def solid_material(grid: CellGrid, material: int = 10,
                   count: int | None = None) -> list[float]:
    return [float(material)] * (grid.active_count if count is None else count)


def surface_step(kind: str, *, rate: float, duration: float, **extra) -> dict:
    step = {
        "id": f"{kind}_step_0001", "name": kind, "enabled": True,
        "operation": kind, "model": "isotropic", "rateMagnitude": rate,
        "duration": duration, "rateMode": "uniform",
    }
    if kind == "deposit":
        step["depositMaterial"] = "SiO2"
    step.update(extra)
    return step


class FieldRemapUnitTests(unittest.TestCase):
    def test_grid_round_trips_through_native_node_points(self) -> None:
        grid = make_grid(dimension=2, delta=0.25, origin=(-0.5, -0.75, 0.0),
                         shape=(5, 4, 1))
        parsed = CellGrid.from_points(grid_nodes(grid), dimension=2, delta=0.25)
        self.assertEqual(parsed.signature(), grid.signature())
        self.assertEqual(parsed.cell_count, 20)
        self.assertEqual(parsed.index(1, 2, 0), 1 + 5 * 2)
        self.assertEqual(parsed.coordinates(1 + 5 * 2), (1, 2, 0))

    def test_from_points_rejects_a_gap_in_the_lattice(self) -> None:
        points = grid_nodes(make_grid(shape=(3, 3, 1)))
        points = [point for point in points if point[0] != 0.5]
        with self.assertRaises(GridRemapError):
            CellGrid.from_points(points, dimension=2, delta=0.5)

    def test_same_grid_is_an_exact_ordered_copy(self) -> None:
        grid = make_grid()
        values = [float(index) for index in range(grid.cell_count)]
        result = remap_cell_fields(
            grid, grid, old_values={"dopant": values},
            old_material=solid_material(grid), new_material=solid_material(grid),
            cover_material_id=3, labels=["dopant"], source_step_id="etch_1")
        self.assertEqual(result.labels["dopant"], values)
        record = result.record
        self.assertEqual(record["method"], "same-grid-index-copy")
        self.assertTrue(record["sameGrid"])
        self.assertEqual(record["remappedCells"], grid.cell_count)
        self.assertEqual(record["newCells"], 0)
        self.assertEqual(record["removedCells"], 0)
        self.assertEqual(record["mass"]["dopant"]["before"], sum(values) * 0.25)
        self.assertEqual(record["mass"]["dopant"]["after"], sum(values) * 0.25)
        self.assertEqual(record["mass"]["dopant"]["lost"], 0.0)
        self.assertEqual(record["labelsPreserved"], True)
        self.assertEqual(record["oldGrid"]["shape"], [4, 4, 1])

    def test_integer_shift_copies_by_absolute_position(self) -> None:
        old = make_grid(shape=(4, 4, 1))
        # New grid starts one cell higher: new row k maps to old row k + 1.
        new = make_grid(origin=(0.0, 0.5, 0.0), shape=(4, 4, 1))
        values = [float(index) for index in range(old.cell_count)]
        result = remap_cell_fields(
            old, new, old_values={"dopant": values},
            old_material=solid_material(old), new_material=solid_material(new),
            cover_material_id=3, labels=["dopant"])
        nx = old.shape[0]
        remapped = result.labels["dopant"]
        self.assertEqual(result.record["method"],
                         "conservative-axis-aligned-integer-overlap")
        self.assertEqual(result.record["cellOffset"], {"x": 0, "y": 1, "z": 0})
        self.assertEqual(result.record["remappedCells"], 12)
        self.assertEqual(result.record["removedByGrid"], 4)
        self.assertEqual(result.record["newCells"], 4)
        for k in range(3):
            self.assertEqual(remapped[nx * k:nx * (k + 1)],
                             values[nx * (k + 1):nx * (k + 2)])
        self.assertEqual(remapped[nx * 3:], [0.0] * nx)
        self.assertEqual(result.record["mass"]["dopant"]["lost"], sum(values[:nx]) * 0.25)

    def test_removed_material_records_mass_loss_without_redistribution(self) -> None:
        grid = make_grid(shape=(4, 4, 1))
        values = [1.0] * grid.cell_count
        new_material = solid_material(grid)
        cover = 3
        nx = grid.shape[0]
        for index in range(grid.index(0, 3, 0), grid.index(nx - 1, 3, 0) + 1):
            new_material[index] = float(cover)
        result = remap_cell_fields(
            grid, grid, old_values={"dopant": values},
            old_material=solid_material(grid), new_material=new_material,
            cover_material_id=cover, labels=["dopant"])
        self.assertEqual(result.labels["dopant"][grid.index(0, 3, 0)], 0.0)
        self.assertEqual(result.labels["dopant"][grid.index(0, 2, 0)], 1.0)
        self.assertEqual(result.record["removedByMaterial"], nx)
        self.assertEqual(result.record["removedCells"], nx)
        self.assertEqual(result.record["mass"]["dopant"]["lost"], nx * 0.25)
        self.assertEqual(
            result.record["mass"]["dopant"]["after"],
            result.record["mass"]["dopant"]["before"] - nx * 0.25)

    def test_reactivated_cells_start_from_the_preserved_value(self) -> None:
        grid = make_grid(shape=(4, 4, 1))
        values = [2.0] * grid.cell_count
        old_material = solid_material(grid)
        nx = grid.shape[0]
        for index in range(grid.index(0, 3, 0), grid.index(nx - 1, 3, 0) + 1):
            old_material[index] = 3.0
        result = remap_cell_fields(
            grid, grid, old_values={"dopant": values},
            old_material=old_material, new_material=solid_material(grid),
            cover_material_id=3, labels=["dopant"])
        self.assertEqual(result.record["reactivatedCells"], nx)
        self.assertEqual(result.record["removedCells"], 0)
        self.assertEqual(result.labels["dopant"][grid.index(0, 3, 0)], 2.0)

    def test_non_integer_shift_is_refused(self) -> None:
        old = make_grid()
        new = make_grid(origin=(0.0, 0.2, 0.0))
        with self.assertRaises(GridRemapError) as context:
            remap_cell_fields(
                old, new, old_values={"dopant": [1.0] * old.cell_count},
                old_material=solid_material(old), new_material=solid_material(new),
                cover_material_id=3, labels=["dopant"])
        self.assertIn("conservative grid remap not implemented", str(context.exception))

    def test_lateral_grid_change_is_refused(self) -> None:
        old = make_grid()
        for changed in (make_grid(origin=(0.5, 0.0, 0.0)),
                        make_grid(shape=(5, 4, 1)),
                        make_grid(delta=0.25, shape=(8, 8, 1))):
            with self.subTest(changed=changed.signature()):
                with self.assertRaises(GridRemapError) as context:
                    check_captured_grid(old, changed)
                self.assertIn("conservative grid remap not implemented",
                              str(context.exception))

    def test_grid_growth_is_conservative_and_new_cells_are_zero(self) -> None:
        old = make_grid(shape=(4, 3, 1))
        new = make_grid(origin=(0.0, -0.5, 0.0), shape=(4, 5, 1))
        values = [float(index + 1) for index in range(old.cell_count)]
        result = remap_cell_fields(
            old, new, old_values={"dopant": values},
            old_material=solid_material(old), new_material=solid_material(new),
            cover_material_id=3, labels=["dopant"])
        nx = old.shape[0]
        self.assertEqual(result.record["cellOffset"], {"x": 0, "y": -1, "z": 0})
        self.assertEqual(result.record["newCells"], 2 * nx)
        self.assertEqual(result.record["removedCells"], 0)
        self.assertEqual(result.labels["dopant"][:nx], [0.0] * nx)
        self.assertEqual(result.labels["dopant"][nx:4 * nx], values)
        self.assertEqual(result.labels["dopant"][4 * nx:], [0.0] * nx)
        self.assertEqual(result.record["mass"]["dopant"]["lost"], 0.0)

    def test_remap_record_is_deterministic(self) -> None:
        grid = make_grid()
        values = [float(index % 5) for index in range(grid.cell_count)]
        materials = solid_material(grid)
        first = remap_cell_fields(
            grid, grid, old_values={"dopant": values}, old_material=materials,
            new_material=materials, cover_material_id=3, labels=["dopant"])
        second = remap_cell_fields(
            grid, grid, old_values={"dopant": values}, old_material=materials,
            new_material=materials, cover_material_id=3, labels=["dopant"])
        self.assertEqual(json.dumps(first.record, sort_keys=True),
                         json.dumps(second.record, sort_keys=True))
        self.assertEqual(first.record["fieldDigests"],
                         {"dopant": hashlib.sha256(
                             array("d", values).tobytes()).hexdigest()})

    def test_pre_allocation_bound_covers_the_known_repro_grids(self) -> None:
        from worker.simulate import _field_grid_cell_bound
        # Repro 1: 1 um / 0.05 um = 20 lateral cells (+2 slack) and
        # 2.0 - (-0.45) = 2.45 um = 49 rows (+4 slack) -> 1,166 >= 980.
        bound = _field_grid_cell_bound(
            {"dimension": 2, "domain": {"gridDelta": 0.05, "xExtent": 1.0}},
            top=2.0, bottom=-0.45, delta=0.05)
        self.assertGreaterEqual(bound, 20 * 49)
        self.assertEqual(bound, 22 * 53)
        # Repro 2: the 0.4 x 0.4 / 0.02 3D grid over the whole band is far
        # above the 2M construction limit once the surface is at 99.9 um.
        bound3 = _field_grid_cell_bound(
            {"dimension": 3, "domain": {"gridDelta": 0.02, "xExtent": 0.4,
                                        "yExtent": 0.4}},
            top=99.9, bottom=-0.42, delta=0.02)
        self.assertGreater(bound3, 2_000_000)

    def sparse_grid(self, shape=(4, 4, 1), *, origin=(0.0, 0.0, 0.0),
                    missing=()):
        missing = set(missing)
        dense = make_grid(shape=shape, origin=origin)
        active = array("q", [key for key in range(dense.cell_count)
                             if key not in missing])
        return dense.with_active(active)

    def test_sparse_same_layout_copies_by_lattice_key(self) -> None:
        grid = self.sparse_grid(missing=(12,))
        values = [float(index) for index in range(grid.active_count)]
        result = remap_cell_fields(
            grid, grid, old_values={"dopant": values},
            old_material=solid_material(grid), new_material=solid_material(grid),
            cover_material_id=3, labels=["dopant"])
        self.assertEqual(result.labels["dopant"], values)
        record = result.record
        self.assertTrue(record["sameGrid"])
        self.assertTrue(record["sparse"])
        self.assertEqual(record["oldCellCount"], 15)
        self.assertEqual(record["oldFullCellCount"], 16)
        self.assertEqual(record["oldMissingCellCount"], 1)
        self.assertEqual(record["remappedCells"], 15)
        self.assertEqual(record["newCells"], 0)
        self.assertEqual(record["removedCells"], 0)
        self.assertEqual(record["mass"]["dopant"]["lost"], 0.0)

    def test_sparse_new_active_cells_start_at_zero(self) -> None:
        old = self.sparse_grid(missing=(12,))
        new = self.sparse_grid()  # dense: key 12 becomes active
        values = [float(index + 1) for index in range(old.active_count)]
        result = remap_cell_fields(
            old, new, old_values={"dopant": values},
            old_material=solid_material(old), new_material=solid_material(new),
            cover_material_id=3, labels=["dopant"])
        self.assertEqual(result.record["newCells"], 1)
        self.assertEqual(result.record["newCellCount"], 16)
        # Linear key 12 is the 13th dense cell; active positions 0..11 keep
        # their values, position 12 is the new cell, 13..15 keep theirs.
        self.assertEqual(result.labels["dopant"][:12], values[:12])
        self.assertEqual(result.labels["dopant"][12], 0.0)
        self.assertEqual(result.labels["dopant"][13:], values[12:])
        self.assertEqual(result.record["mass"]["dopant"]["lost"], 0.0)

    def test_sparse_removed_active_cell_records_grid_loss(self) -> None:
        old = self.sparse_grid()
        new = self.sparse_grid(missing=(12,))
        values = [2.0] * old.active_count
        result = remap_cell_fields(
            old, new, old_values={"dopant": values},
            old_material=solid_material(old), new_material=solid_material(new),
            cover_material_id=3, labels=["dopant"])
        self.assertEqual(result.record["removedByGrid"], 1)
        self.assertEqual(result.record["removedByMaterial"], 0)
        self.assertEqual(result.record["removedCells"], 1)
        self.assertEqual(result.record["mass"]["dopant"]["lost"], 2.0 * 0.25)
        self.assertEqual(result.record["newCells"], 0)

    def test_sparse_integer_shift_maps_by_lattice_key(self) -> None:
        old = self.sparse_grid(shape=(4, 4, 1), missing=(12,))
        shifted = array("q", sorted({int(key) - 4 for key in old.active
                                     if int(key) - 4 >= 0}))
        new = make_grid(shape=(4, 4, 1), origin=(0.0, 0.5, 0.0)).with_active(shifted)
        values = [float(index + 1) for index in range(old.active_count)]
        result = remap_cell_fields(
            old, new, old_values={"dopant": values},
            old_material=solid_material(old), new_material=solid_material(new),
            cover_material_id=3, labels=["dopant"])
        source = {int(key): values[position]
                  for position, key in enumerate(old.active)}
        for position, key in enumerate(new.active):
            expected = source.get(int(key) + 4, 0.0)
            self.assertEqual(result.labels["dopant"][position], expected)
        self.assertEqual(result.record["cellOffset"], {"x": 0, "y": 1, "z": 0})
        self.assertGreater(result.record["oldMissingCellCount"], 0)


class FieldRemapWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(2400):
            result = json_body(await asgi_request("/api/runs/" + run_id))
            if result["status"] in ("succeeded", "failed", "cancelled", "timed_out",
                                    "interrupted"):
                return result
            await asyncio.sleep(0.05)
        self.fail("native child exceeded acceptance timeout")

    async def run_project(self, value: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        result = await self.wait(run_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        return run_id, storage.read_json(storage.run_path(run_id) / "manifest.json")

    def summary(self, run_id: str, step_id: str = "analysis_step_01") -> dict:
        return storage.read_json(storage.run_path(run_id) / "artifacts" /
                                 f"{step_id}-cell-set-fields.json")

    def snapshot(self, run_id: str, step_id: str) -> dict:
        snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(storage.run_path(run_id) / f"{step_id}.json")

    def assert_mass_balance(self, record: dict) -> None:
        for label, mass in record["mass"].items():
            with self.subTest(label=label):
                self.assertAlmostEqual(mass["before"] - mass["after"], mass["lost"],
                                       places=6)
                self.assertAlmostEqual(
                    record["mass"][label]["after"],
                    record["mass"][label]["before"] - record["mass"][label]["lost"],
                    places=6)

    async def test_implant_etch_anneal_analysis_2d_records_the_remap(self) -> None:
        steps = [implant_step(), surface_step("etch", rate=0.5, duration=0.1),
                 anneal_step(), analysis_step()]
        run_id, _manifest = await self.run_project(project(2, steps=steps))
        payload = self.summary(run_id)
        setup = payload["cellSetSetup"]
        self.assertEqual(setup["remapCount"], 1)
        record = setup["remaps"][0]
        self.assertEqual(record["sourceStepId"], "etch_step_0001")
        self.assertEqual(record["remapOfStepId"], "implant_step_01")
        self.assertEqual(record["method"], "same-grid-index-copy")
        self.assertTrue(record["exact"])
        self.assertEqual(record["oldCellCount"], record["newCellCount"])
        self.assertEqual(record["remappedCells"], record["oldCellCount"])
        self.assertEqual(record["newCells"], 0)
        self.assertGreater(record["removedCells"], 0)
        self.assertEqual(record["removedByGrid"], 0)
        self.assertGreater(record["mass"]["P_total"]["lost"], 0.0)
        self.assertLess(record["mass"]["P_total"]["after"],
                        record["mass"]["P_total"]["before"])
        self.assert_mass_balance(record)
        self.assertEqual(record["labels"], ["P_total", "P_damage", "P_damage_last"])
        self.assertEqual(record["oldGrid"], record["capturedGrid"])
        # The remapped labels are still present and the analysis still runs.
        self.assertIn("P_active", payload["fieldDigests"])
        self.assertIn("net_doping", payload["fieldDigests"])
        self.assertGreater(payload["process"]["analysis"]["sheetResistance"][0]
                           ["sheetResistanceOhmPerSquare"], 0.0)
        # The surface snapshot itself is not part of the field grid.
        snapshot = self.snapshot(run_id, "etch_step_0001")
        self.assertEqual(snapshot["stepId"], "etch_step_0001")
        self.assertNotIn("cellSetProcess", snapshot)
        # Some dopant survived the etch: mass is not silently redistributed.
        self.assertGreater(record["mass"]["P_total"]["after"], 0.0)

    async def test_three_dimensional_etch_remap_uses_the_same_native_path(self) -> None:
        steps = []
        for step in (implant_step(), surface_step("etch", rate=0.5, duration=0.1),
                     anneal_step(), analysis_step()):
            step = copy.deepcopy(step)
            if "cellSet" in step:
                step["cellSet"]["height"] = 0.2
                step["cellSet"]["substrateDepth"] = 0.3
            steps.append(step)
        value = project(3, steps=steps)
        value["domain"] = {"gridDelta": 0.02, "xExtent": 0.4, "yExtent": 0.4,
                           "boundaryXY": "reflective"}
        run_id, _manifest = await self.run_project(value)
        payload = self.summary(run_id)
        record = payload["cellSetSetup"]["remaps"][0]
        self.assertEqual(record["sourceStepId"], "etch_step_0001")
        self.assertEqual(record["method"], "same-grid-index-copy")
        self.assertEqual(record["oldCellCount"], record["newCellCount"])
        self.assertGreater(record["removedCells"], 0)
        self.assert_mass_balance(record)
        self.assertEqual(payload["dimension"], 3)
        self.assertEqual(payload["grid"]["nativeElementType"], "hexahedron")
        self.assertEqual(record["cellOffset"], {"x": 0, "y": 0, "z": 0})
        analysis = self.snapshot(run_id, "analysis_step_01")["cellSetProcess"]
        self.assertEqual(analysis["cellSet"]["remapCount"], 1)
        self.assertEqual(analysis["cellSet"]["periodicBoundary"], [False, False, False])

    async def test_implant_deposit_anneal_preserves_the_dopant_mass(self) -> None:
        steps = [implant_step(),
                 surface_step("deposit", rate=0.5, duration=0.1),
                 anneal_step(), analysis_step()]
        run_id, _manifest = await self.run_project(project(2, steps=steps))
        payload = self.summary(run_id)
        record = payload["cellSetSetup"]["remaps"][0]
        self.assertEqual(record["sourceStepId"], "deposit_step_0001")
        self.assertEqual(record["method"], "same-grid-index-copy")
        self.assertEqual(record["removedCells"], 0)
        self.assertEqual(record["newCells"], 0)
        self.assertEqual(record["mass"]["P_total"]["lost"], 0.0)
        self.assertEqual(record["mass"]["P_total"]["after"],
                         record["mass"]["P_total"]["before"])
        self.assertEqual(record["mass"]["P_damage"]["lost"], 0.0)
        self.assertEqual(payload["cellSetSetup"]["coverMaterial"], "GAS")
        self.assertIn("P_active", payload["fieldDigests"])

    async def test_deep_etch_grows_the_grid_conservatively(self) -> None:
        steps = [implant_step(cellSet={"height": 0.5, "substrateDepth": 0.2,
                                       "coverMaterial": "GAS", "periodic": False,
                                       "format": "both"}),
                 surface_step("etch", rate=1.0, duration=0.3),
                 anneal_step(cellSet={"height": 0.5, "substrateDepth": 0.2,
                                      "coverMaterial": "GAS", "periodic": False,
                                      "format": "both"}),
                 analysis_step(cellSet={"height": 0.5, "substrateDepth": 0.2,
                                        "coverMaterial": "GAS", "periodic": False,
                                        "format": "both"})]
        run_id, _manifest = await self.run_project(project(2, steps=steps))
        payload = self.summary(run_id)
        record = payload["cellSetSetup"]["remaps"][0]
        self.assertEqual(record["method"],
                         "conservative-axis-aligned-integer-overlap")
        self.assertFalse(record["sameGrid"])
        self.assertGreater(record["newCellCount"], record["oldCellCount"])
        self.assertGreater(record["newCells"], 0)
        self.assertEqual(record["removedByGrid"], 0)
        self.assertLess(record["newGrid"]["bottom"], record["capturedGrid"]["bottom"])
        self.assertEqual(record["newGrid"]["top"], record["capturedGrid"]["top"])
        self.assert_mass_balance(record)

    async def test_replay_reproduces_the_remap_record(self) -> None:
        steps = [implant_step(), surface_step("etch", rate=0.5, duration=0.1),
                 anneal_step(), analysis_step()]
        run_id, _manifest = await self.run_project(project(2, steps=steps))
        original = self.summary(run_id)
        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(submit[0], 202, submit[2])
        replay_id = json_body(submit)["runId"]
        result = await self.wait(replay_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        replayed = self.summary(replay_id)
        self.assertEqual(original["cellSetSetup"]["remaps"],
                         replayed["cellSetSetup"]["remaps"])
        self.assertEqual(original["fieldDigests"], replayed["fieldDigests"])
        self.assertEqual(original["process"]["analysis"]["sheetResistance"],
                         replayed["process"]["analysis"]["sheetResistance"])

    async def test_boolean_growth_between_field_steps_stays_within_the_bound(self) -> None:
        settings = field_settings()
        value = project(2, steps=[implant_step(cellSet=dict(settings)),
                                  boolean_step(),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["domain"] = {"gridDelta": 0.05, "xExtent": 1.0,
                           "xBoundary": "reflective"}
        run_id, _manifest = await self.run_project(value)
        payload = self.summary(run_id)
        setup = payload["cellSetSetup"]
        record = setup["remaps"][0]
        self.assertEqual(record["sourceStepId"], "boolean_step_01")
        self.assertEqual(record["remapOfStepId"], "implant_step_01")
        # The union box lifts the surface to 2.0 um. The regenerated grid keeps
        # the captured bottom (-0.45) and spans up to the new top.
        self.assertEqual(record["oldCellCount"], 380)
        self.assertEqual(record["newCellCount"], 980)
        self.assertEqual(record["newGrid"]["top"], 2.0)
        self.assertEqual(record["newGrid"]["bottom"], -0.45)
        self.assertEqual(record["method"],
                         "conservative-axis-aligned-integer-overlap")
        self.assertEqual(record["cellOffset"], {"x": 0, "y": 0, "z": 0})
        self.assertEqual(record["newCells"], 600)
        self.assertEqual(record["removedCells"], 0)
        self.assertGreater(record["reactivatedCells"], 0)
        self.assertEqual(record["mass"]["P_total"]["lost"], 0.0)
        self.assert_mass_balance(record)
        # The pre-allocation bound covers the real native allocation.
        self.assertEqual(setup["numberOfCells"], 980)
        self.assertGreaterEqual(setup["cellsUpperBound"], 980)

    async def test_grid_growth_above_the_construction_limit_is_refused_before_allocation(self) -> None:
        settings = field_settings()
        value = project(3, steps=[implant_step(cellSet=dict(settings)),
                                  boolean_step(height=100.0, depth=4.0),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["domain"] = {"gridDelta": 0.02, "xExtent": 0.4, "yExtent": 0.4,
                           "boundaryXY": "reflective"}
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        result = await self.wait(run_id)
        self.assertEqual(result["status"], "failed")
        log = (storage.run_path(run_id) / "run.log").read_text(encoding="utf-8",
                                                               errors="replace")
        self.assertIn("construction limit", log)
        self.assertIn("2,000,000", log)
        # The rejection happens before the dense allocation, not against a
        # post-allocation bound.
        self.assertNotIn("DenseCellSet generation produced", log)
        artifacts = storage.run_path(run_id) / "artifacts"
        self.assertFalse((artifacts / "analysis_step_01-cell-set-fields.json").exists())
        self.assertFalse((artifacts / "boolean_step_01-cell-set-fields.json").exists())

    async def test_planarize_before_the_first_field_step_is_allowed_2d(self) -> None:
        settings = field_settings()
        value = project(2, steps=[planarize_step(),
                                  implant_step(cellSet=dict(settings)),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["geometry"] = {"kind": "plane", "height": 2.0,
                             "substrateMaterial": "Si"}
        value["domain"] = {"gridDelta": 0.05, "xExtent": 1.0,
                           "xBoundary": "reflective"}
        run_id, _manifest = await self.run_project(value)
        payload = self.summary(run_id)
        # plane(2) -> planarize(0) -> implant on a 0.5/0.4 grid, not on the
        # original 2 um surface: 20 lateral * 19 rows.
        self.assertEqual(payload["numberOfCells"], 380)
        self.assertAlmostEqual(payload["cellSetSetup"]["grid"]["top"], 0.5)
        self.assertAlmostEqual(payload["cellSetSetup"]["grid"]["bottom"], -0.45)
        self.assertEqual(payload["cellSetSetup"]["remapCount"], 0)

    async def test_planarize_before_the_first_field_step_is_allowed_3d(self) -> None:
        settings = field_settings()
        value = project(3, steps=[planarize_step(),
                                  implant_step(cellSet=dict(settings)),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["geometry"] = {"kind": "plane", "height": 2.0,
                             "substrateMaterial": "Si"}
        value["domain"] = {"gridDelta": 0.02, "xExtent": 0.4, "yExtent": 0.4,
                           "boundaryXY": "reflective"}
        run_id, _manifest = await self.run_project(value)
        payload = self.summary(run_id)
        self.assertEqual(payload["numberOfCells"], 18_400)
        self.assertAlmostEqual(payload["cellSetSetup"]["grid"]["top"], 0.5)
        self.assertAlmostEqual(payload["cellSetSetup"]["grid"]["bottom"], -0.42)

    async def test_sparse_fin_implant_deposit_anneal_2d(self) -> None:
        settings = field_settings(height=0.6)
        value = project(2, steps=[implant_step(cellSet=dict(settings)),
                                  surface_step("deposit", rate=0.5, duration=0.1),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["geometry"] = fin_geometry(2)
        value["domain"] = {"gridDelta": 0.05, "xExtent": 1.0,
                           "xBoundary": "reflective"}
        run_id, _manifest = await self.run_project(value)
        implant_setup = self.snapshot(run_id, "implant_step_01")["cellSetProcess"]["cellSet"]
        self.assertTrue(implant_setup["sparse"], implant_setup)
        self.assertLess(implant_setup["activeCellCount"],
                        implant_setup["fullCellCount"])
        payload = self.summary(run_id)
        setup = payload["cellSetSetup"]
        self.assertEqual(setup["remapCount"], 1)
        record = setup["remaps"][0]
        self.assertEqual(record["sourceStepId"], "deposit_step_0001")
        self.assertTrue(record["sparse"])
        self.assertEqual(record["method"],
                         "conservative-axis-aligned-integer-overlap")
        self.assertGreater(record["oldFullCellCount"], record["oldCellCount"])
        self.assertGreater(record["newCells"], 0)
        self.assertEqual(record["removedByGrid"], 0)
        self.assert_mass_balance(record)
        self.assertGreater(record["mass"]["P_total"]["after"], 0.0)
        self.assertIn("P_active", payload["fieldDigests"])
        self.assertIn("net_doping", payload["fieldDigests"])

    async def test_sparse_fin_implant_etch_anneal_3d(self) -> None:
        settings = field_settings(height=0.6)
        value = project(3, steps=[implant_step(cellSet=dict(settings)),
                                  surface_step("etch", rate=0.5, duration=0.1),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["geometry"] = fin_geometry(3)
        value["domain"] = {"gridDelta": 0.05, "xExtent": 0.6, "yExtent": 0.6,
                           "boundaryXY": "reflective"}
        run_id, _manifest = await self.run_project(value)
        implant_setup = self.snapshot(run_id, "implant_step_01")["cellSetProcess"]["cellSet"]
        self.assertTrue(implant_setup["sparse"], implant_setup)
        self.assertLess(implant_setup["activeCellCount"],
                        implant_setup["fullCellCount"])
        payload = self.summary(run_id)
        record = payload["cellSetSetup"]["remaps"][0]
        self.assertEqual(record["sourceStepId"], "etch_step_0001")
        self.assertTrue(record["sparse"])
        self.assertGreater(record["oldFullCellCount"], record["oldCellCount"])
        self.assertGreater(record["newCells"], 0)
        self.assertGreater(record["mass"]["P_total"]["lost"], 0.0)
        self.assert_mass_balance(record)
        self.assertEqual(payload["dimension"], 3)
        self.assertEqual(payload["grid"]["nativeElementType"], "hexahedron")



class FieldRemapGeometryLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    def test_three_dimensional_surface_above_the_field_height_is_a_422(self) -> None:
        settings = field_settings()
        value = project(3, steps=[implant_step(cellSet=dict(settings)),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["domain"] = {"gridDelta": 0.02, "xExtent": 0.4, "yExtent": 0.4,
                           "boundaryXY": "reflective"}
        value["geometry"] = {"kind": "plane", "height": 101.0,
                             "substrateMaterial": "Si"}
        with self.assertRaises(ProjectInputError) as context:
            validate_project(value)
        message = str(context.exception)
        self.assertIn("below the highest initial surface point", message)
        self.assertIn("101", message)
        self.assertIn("steps[0].cellSet.height", message)

    def test_deep_initial_surface_is_refused_by_the_static_cell_bound(self) -> None:
        from backend.implant_models import field_geometry_issues
        steps = [implant_step(cellSet=field_settings(height=0.6, substrateDepth=0.4))]
        issues = field_geometry_issues(
            steps, geometry={"kind": "trench", "depth": 100.0, "maskHeight": 0.2},
            dimension=3, grid_delta=0.02, x_extent=0.4, y_extent=0.4)
        self.assertTrue(any("construction limit" in message
                            for _index, _path, message in issues), issues)

    def test_height_below_the_surface_is_refused_but_a_gds_extent_is_left_to_the_worker(self) -> None:
        from backend.implant_models import field_geometry_issues
        steps = [implant_step(cellSet=field_settings())]
        issues = field_geometry_issues(
            steps, geometry={"kind": "plane", "height": 5.0}, dimension=2,
            grid_delta=0.01, x_extent=1.0, y_extent=0.0)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0][1], "cellSet.height")
        self.assertEqual(field_geometry_issues(
            steps, geometry={"kind": "gds", "assetId": "0" * 64}, dimension=2,
            grid_delta=0.01, x_extent=1.0, y_extent=0.0), [])
        self.assertEqual(field_geometry_issues(
            steps, geometry={"kind": "plane", "height": 5.0}, dimension=2,
            grid_delta=0.01, x_extent=1.0, y_extent=0.0, from_domain_state=True), [])


class FieldRemapDirectWorkerBounds(unittest.TestCase):
    """A hand-edited input.json cannot bypass the pre-allocation checks."""

    def run_worker(self, project: dict, timeout: int = 240) -> tuple[int, str, dict, Path]:
        workdir = Path(tempfile.mkdtemp(prefix="field-bound-", dir=str(ROOT / "var")))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        config = {
            "kind": "simulate", "runId": "field-bound", "project": project,
            "runSeed": 271828,
            "expectedVersions": {"viennaps": version("viennaps"),
                                 "viennals": version("viennals")},
        }
        input_path = workdir / "input.json"
        input_path.write_text(json.dumps(config), encoding="utf-8")
        run_dir = workdir / "run"
        run_dir.mkdir()
        completed = subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "worker/simulate.py"),
             str(input_path), str(run_dir)],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        result_path = run_dir / "worker-result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        return completed.returncode, completed.stdout + completed.stderr, result, run_dir

    def test_direct_worker_refuses_height_below_the_surface_before_allocating(self) -> None:
        settings = field_settings()
        value = project(3, steps=[implant_step(cellSet=dict(settings)),
                                  anneal_step(cellSet=dict(settings)),
                                  analysis_step(cellSet=dict(settings))])
        value["domain"] = {"gridDelta": 0.02, "xExtent": 0.4, "yExtent": 0.4,
                           "boundaryXY": "reflective"}
        value = copy.deepcopy(validate_project(value)[0].model_dump(mode="json",
                                                                    exclude_none=True))
        value["geometry"] = {"kind": "plane", "height": 101.0,
                             "substrateMaterial": "Si"}
        code, output, result, run_dir = self.run_worker(value)
        self.assertNotEqual(code, 0, output)
        self.assertNotIn("WORKER_COMPLETE", output)
        self.assertEqual(result, {})
        self.assertIn("below the highest", output)
        self.assertIn("initial surface point", output)
        self.assertNotIn("DenseCellSet generation produced", output)
        self.assertFalse((run_dir / "artifacts").exists())


class _CountingPs:
    """Proxy that fails if the worker allocates a native Domain too early."""

    def __init__(self, real) -> None:
        self._real = real
        self.domain_calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Domain(self, *args, **kwargs):
        self.domain_calls += 1
        raise AssertionError("native Domain created before the pre-allocation bound")


class FieldRemapWideBandTests(unittest.TestCase):
    """A stored level-set band wider than the requested Expand must be bounded."""

    def source_domain(self, width: int = 202):
        import viennaps as ps
        import viennals as ls
        ps.setDimension(2)
        delta = 0.05
        domain = ps.Domain(delta, 1.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
        ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
        for level_set in domain.getLevelSets():
            ls.Expand(level_set, width).apply()
        project = {"dimension": 2,
                   "domain": {"gridDelta": delta, "xExtent": 1.0},
                   "schemaVersion": 7}
        return ps, domain, project

    def test_pre_expanded_band_is_inside_the_pre_allocation_bound(self) -> None:
        from worker.simulate import _generate_field_cell_set
        ps, domain, project = self.source_domain()
        _fd, cell_set, grid, info = _generate_field_cell_set(
            ps, domain, project, height=0.5, band_cells=18,
            cover=ps.Material.GAS, periodic=False)
        # Expand(18) keeps the 202-point stored band: 20 lateral * 111 rows.
        self.assertEqual(cell_set.getNumberOfCells(), 2220)
        self.assertEqual(info["storedBandHalfCells"], 101)
        self.assertEqual(info["bandHalfCells"], 101)
        self.assertGreaterEqual(info["cellsUpperBound"], 2220)
        self.assertAlmostEqual(grid.bottom, -5.05, places=9)
        self.assertAlmostEqual(grid.top, 0.5, places=9)

    def test_odd_stored_band_uses_the_measured_floor_half_width(self) -> None:
        from worker.simulate import _generate_field_cell_set, _stored_band_half_cells
        ps, domain, project = self.source_domain(width=19)
        self.assertEqual(_stored_band_half_cells(domain), 9)
        _fd, cell_set, grid, info = _generate_field_cell_set(
            ps, domain, project, height=0.5, band_cells=2,
            cover=ps.Material.GAS, periodic=False)
        self.assertEqual(cell_set.getNumberOfCells(), 380)
        self.assertEqual(info["storedBandHalfCells"], 9)
        self.assertEqual(info["bandHalfCells"], 9)
        self.assertAlmostEqual(grid.bottom, -0.45, places=9)

    def test_pre_expanded_band_above_the_cap_is_refused_before_allocating(self) -> None:
        from worker.simulate import _generate_field_cell_set
        ps, domain, project = self.source_domain()
        proxy = _CountingPs(ps)
        with patch("worker.simulate.MAX_CELL_SET_CELLS", 1000):
            with self.assertRaises(RuntimeError) as context:
                _generate_field_cell_set(proxy, domain, project, height=0.5,
                                         band_cells=18, cover=ps.Material.GAS,
                                         periodic=False)
        self.assertIn("construction limit", str(context.exception))
        self.assertEqual(proxy.domain_calls, 0)


class FieldRemapSparseNativeTests(unittest.TestCase):
    """The native cell set of a structured geometry is not a full box."""

    def build(self, dimension: int):
        import viennaps as ps
        ps.setDimension(dimension)
        delta = 0.05
        if dimension == 2:
            domain = ps.Domain(delta, 1.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
            ps.MakeFin(domain, finWidth=0.4, finHeight=0.3, finTaperAngle=0.0,
                       maskHeight=0.2, maskTaperAngle=0.0, halfFin=False,
                       material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
            project = {"dimension": 2,
                       "domain": {"gridDelta": delta, "xExtent": 1.0},
                       "schemaVersion": 7}
        else:
            domain = ps.Domain(delta, 0.6, 0.6,
                               ps.BoundaryType.REFLECTIVE_BOUNDARY)
            ps.d3.MakeFin(domain, finWidth=0.2, finHeight=0.3, finTaperAngle=0.0,
                          maskHeight=0.2, maskTaperAngle=0.0, halfFin=False,
                          material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
            project = {"dimension": 3,
                       "domain": {"gridDelta": delta, "xExtent": 0.6,
                                  "yExtent": 0.6},
                       "schemaVersion": 7}
        return ps, domain, project

    def test_fin_cell_set_is_sparse_not_rejected_as_a_regular_box(self) -> None:
        from worker.simulate import _generate_field_cell_set
        # The 1,860,000-cell FinFET bounding lattice is 100x100x186; this fin
        # reproduces the sparse shape at a test-sized grid. The exact active
        # count depends on native global state left by other modules, so only
        # the sparse invariant and the bounding lattice are pinned.
        expected_full = {2: 660, 3: 4752}
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                ps, domain, project = self.build(dimension)
                _fd, cell_set, grid, info = _generate_field_cell_set(
                    ps, domain, project, height=0.6, band_cells=18,
                    cover=ps.Material.GAS, periodic=False)
                active = int(cell_set.getNumberOfCells())
                full = expected_full[dimension]
                self.assertTrue(info["sparse"])
                self.assertGreater(active, 0)
                self.assertLess(active, full)
                self.assertEqual(info["activeCellCount"], active)
                self.assertEqual(info["fullCellCount"], full)
                self.assertEqual(info["missingCellCount"], full - active)
                self.assertEqual(grid.active_count, active)
                self.assertEqual(grid.cell_count, full)
                signature = grid.signature()
                self.assertEqual(signature["numberOfCells"], active)
                self.assertEqual(signature["fullCellCount"], full)
                self.assertEqual(signature["missingCellCount"], full - active)
                self.assertTrue(signature["sparse"])


class FieldRemapSchemaTests(unittest.TestCase):
    def test_a_surface_step_between_field_steps_is_accepted(self) -> None:
        steps = [implant_step(), surface_step("etch", rate=0.5, duration=0.1),
                 anneal_step(), analysis_step()]
        parsed, warnings = validate_project(project(2, steps=steps))
        self.assertEqual([step["model"] for step in
                          parsed.model_dump(mode="json")["steps"]],
                         ["implant", "isotropic", "anneal", "analysis"])
        self.assertEqual(warnings, [])

    def test_the_shared_grid_parameters_are_still_validated(self) -> None:
        mismatched = [implant_step(),
                      surface_step("etch", rate=0.5, duration=0.1),
                      anneal_step(cellSet={"height": 0.3, "substrateDepth": 0.4,
                                           "coverMaterial": "GAS", "periodic": False,
                                           "format": "both"})]
        with self.assertRaises(Exception) as context:
            validate_project(project(2, steps=mismatched))
        self.assertIn("height must match", str(context.exception))

    def test_analysis_before_an_implant_is_still_refused(self) -> None:
        steps = [analysis_step(), surface_step("etch", rate=0.5, duration=0.1),
                 implant_step()]
        with self.assertRaises(Exception) as context:
            validate_project(project(2, steps=steps))
        self.assertIn("earlier", str(context.exception))


if __name__ == "__main__":
    unittest.main()
