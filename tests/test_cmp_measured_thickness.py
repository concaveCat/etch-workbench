"""Material-aware CMP and measured per-step thickness.

Two related P1/P2 contracts are covered here:

* ``planarize`` can stop on a material (``stopMaterial``/``overPolish``)
  instead of a hand-tuned ``cutoffHeight``. The installed ViennaPS 4.6.2
  ``Planarize`` accepts a height only, so the workbench derives that height
  from the measured top of the stop material and records the derivation.
* Every executed step records what it really did to each material's exposed
  top (``measuredRemoval``/``measuredAdded``), because a particle model's
  nominal rate x duration is a calibrated flux quantity, not a line speed.
"""
from __future__ import annotations

import asyncio
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import snapshots as snapshot_store
from backend import storage
from backend.hashing import requested_input
from backend.main import app
from backend.models import PlanarizeStepV5
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body
from worker.simulate import apply_geometry_operation, measure_material_tops, measure_step_change

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FINFET_PROJECT = FIXTURES / "finfet22_project.json"
FINFET_GDS = FIXTURES / "finfet22_workbench.gds"


def cmp_project() -> dict:
    """SiO2 field with a trench, buried in a TiN/W overburden, then CMP."""
    return {
        "schemaVersion": 5, "name": "材质感知 CMP 回归", "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.02, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "trench", "width": 1.0, "depth": 0.6, "taperAngle": 0.0,
                     "maskHeight": 0.0, "maskTaperAngle": 0.0, "substrateMaterial": "SiO2"},
        "steps": [
            {"id": "cmp_tin_barrier", "name": "TiN 势垒", "enabled": True, "operation": "deposit",
             "model": "isotropic", "rateMagnitude": 0.02, "duration": 2.5, "depositMaterial": "TiN"},
            {"id": "cmp_w_fill", "name": "W 填充", "enabled": True, "operation": "deposit",
             "model": "isotropic", "rateMagnitude": 0.1, "duration": 5.0, "depositMaterial": "W"},
            {"id": "cmp_w_cmp", "name": "W CMP 停在 SiO2", "enabled": True, "model": "planarize",
             "stopMaterial": "SiO2", "overPolish": 0},
            {"id": "cmp_zero_rate", "name": "零速率复查", "enabled": True, "operation": "etch",
             "model": "isotropic", "rateMagnitude": 0, "duration": 1.0},
        ],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu",
                   "timeoutSeconds": 120},
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def cmp_bimodal_project() -> dict:
    """Two SiO2 plateaus under a TiN/W overburden, lower plateau larger.

    A SiO2 layer is deposited on a SiO2 substrate (two level sets of the same
    material, so both their interface and the exposed substrate count as SiO2
    up-facing) and a 2.4 um window is cut through it with a Boolean box. SiO2 is
    then exposed at 0.0 across the whole 4 um width and at 0.3 on the 1.6 um
    field, which mirrors the 22 nm FinFET ``ff22_w_fill`` surface where STI
    (0.036) covers more area than ILD (0.095). The median picks the lower
    plateau; the CMP stop height must pick the upper one.
    """
    return {
        "schemaVersion": 5, "name": "双峰 SiO2 材质感知 CMP 回归", "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.02, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "SiO2"},
        "steps": [
            {"id": "bimodal_ild_dep", "name": "ILD SiO2", "enabled": True, "operation": "deposit",
             "model": "isotropic", "rateMagnitude": 0.1, "duration": 3.0, "depositMaterial": "SiO2"},
            {"id": "bimodal_window", "name": "开窗露出下方 SiO2", "enabled": True, "model": "boolean",
             "booleanOperation": "difference", "position": [0.0, 0.0, 0.0], "width": 2.4,
             "height": 0.4, "angle": 0.0, "applyToAll": True},
            {"id": "bimodal_tin", "name": "TiN 势垒", "enabled": True, "operation": "deposit",
             "model": "isotropic", "rateMagnitude": 0.02, "duration": 2.5, "depositMaterial": "TiN"},
            {"id": "bimodal_w", "name": "W 覆盖", "enabled": True, "operation": "deposit",
             "model": "isotropic", "rateMagnitude": 0.1, "duration": 3.0, "depositMaterial": "W"},
            {"id": "bimodal_cmp", "name": "CMP 停在 SiO2 上方平台", "enabled": True,
             "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.003},
        ],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu",
                   "timeoutSeconds": 120},
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def cmp_project_3d() -> dict:
    """A flat 3D SiO2 field buried under TiN/W, then CMP stopped on SiO2."""
    return {
        "schemaVersion": 7, "name": "3D 材质感知 CMP 回归", "dimension": 3,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.1, "xExtent": 2.0, "yExtent": 2.0, "boundaryXY": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "SiO2"},
        "steps": [
            {"id": "step_cmp3d_tin", "name": "TiN", "enabled": True, "operation": "deposit", "model": "isotropic",
             "rateMagnitude": 0.05, "duration": 1.0, "depositMaterial": "TiN"},
            {"id": "step_cmp3d_w", "name": "W", "enabled": True, "operation": "deposit", "model": "isotropic",
             "rateMagnitude": 0.1, "duration": 2.0, "depositMaterial": "W"},
            {"id": "step_cmp3d_cmp", "name": "CMP 停在 SiO2", "enabled": True, "model": "planarize",
             "stopMaterial": "SiO2", "overPolish": 0.0},
        ],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu",
                   "timeoutSeconds": 120},
        "view3D": {"projection": "parallel", "camera": None, "displayMode": "surface", "visibleMaterialIds": [],
                   "opacityByMaterial": {},
                   "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                   "section": {"enabled": False, "axis": "y", "position": 0.0}},
        "outputSettings": {"levelSets": False, "levelSetMeshes": False, "surfaceMeshVtk": False,
                           "volumeMesh": False, "nativeMetadata": False},
    }


def up_facing_segments(snapshot: dict, material: str) -> list[tuple[float, float, float]]:
    """Return (xMin, xMax, length) for every up-facing cell of one material."""
    ids = {int(key): row["name"] for key, row in snapshot["materials"].items()}
    material_id = next(key for key, name in ids.items() if name == material)
    segments = []
    for (first, second), cell_material in zip(snapshot["lines"], snapshot["cellData"]["materialIds"]):
        if int(cell_material) != material_id:
            continue
        ax, ah = snapshot["points"][first][0], snapshot["points"][first][1]
        bx, bh = snapshot["points"][second][0], snapshot["points"][second][1]
        dh = bh - ah
        length = math.hypot(bx - ax, dh)
        if length <= 0.0 or abs(dh) > 0.5 * length:
            continue
        segments.append((min(ax, bx), max(ax, bx), length))
    return segments


class PlanarizeStopMaterialModel(unittest.TestCase):
    def test_stop_material_step_accepts_the_documented_field_set(self):
        step = PlanarizeStepV5(id="cmp_step_1", name="CMP", enabled=True, model="planarize",
                               stopMaterial="SiO2", overPolish=0.005)
        self.assertIsNone(step.cutoffHeight)
        self.assertEqual(step.stopMaterial, "SiO2")
        self.assertEqual(step.overPolish, 0.005)

    def test_exactly_one_cutoff_source_is_required(self):
        with self.assertRaises(ValueError):
            PlanarizeStepV5(id="cmp_step_1", name="CMP", enabled=True, model="planarize")
        with self.assertRaises(ValueError):
            PlanarizeStepV5(id="cmp_step_1", name="CMP", enabled=True, model="planarize",
                            cutoffHeight=0.1, stopMaterial="SiO2")
        with self.assertRaises(ValueError):
            # overPolish is meaningless without a stop material.
            PlanarizeStepV5(id="cmp_step_1", name="CMP", enabled=True, model="planarize",
                            cutoffHeight=0.1, overPolish=0.01)
        with self.assertRaises(ValueError):
            PlanarizeStepV5(id="cmp_step_1", name="CMP", enabled=True, model="planarize",
                            stopMaterial="SiO2", overPolish=-0.001)

    def test_existing_planarize_step_hash_input_is_unchanged(self):
        # The canonical request-hash input must not gain keys for a step that
        # does not use the new fields; otherwise every saved run changes hash.
        aware = validate_project(cmp_project())[0]
        aware_steps = requested_input(aware)["steps"]
        self.assertEqual(aware_steps[2].get("model"), "planarize")
        self.assertEqual(aware_steps[2].get("stopMaterial"), "SiO2")
        self.assertEqual(aware_steps[2].get("overPolish"), 0)

        historical = cmp_project()
        historical["steps"][2] = {"id": "cmp_w_cmp", "name": "W CMP", "enabled": True,
                                  "model": "planarize", "cutoffHeight": 0.0}
        plain = validate_project(historical)[0]
        self.assertEqual(requested_input(plain)["steps"][2], {"model": "planarize", "cutoffHeight": 0.0})

        self.assertNotEqual(requested_input(plain)["steps"][2], requested_input(aware)["steps"][2])


class PlanarizeStopMaterialValidation(unittest.TestCase):
    def test_unknown_stop_material_is_rejected(self):
        project = cmp_project()
        project["steps"][2]["stopMaterial"] = "Unobtainium"
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        self.assertTrue(any(issue.path == "steps[2].stopMaterial" for issue in ctx.exception.issues))

    def test_stop_material_must_exist_when_the_step_runs(self):
        project = cmp_project()
        project["steps"][2]["stopMaterial"] = "Si3N4"
        with self.assertRaises(ProjectInputError) as ctx:
            validate_project(project)
        messages = [issue.message for issue in ctx.exception.issues if issue.path == "steps[2].stopMaterial"]
        self.assertTrue(messages)
        self.assertIn("not present when this step runs", messages[0])

    def test_material_from_an_earlier_deposition_is_accepted_with_a_limitation_warning(self):
        project = cmp_project()
        project["steps"][2]["stopMaterial"] = "TiN"
        parsed, warnings = validate_project(project, kind="simulate")
        self.assertEqual(parsed.steps[2].stopMaterial, "TiN")
        self.assertTrue(any("global cutoff" in warning and "lateral selectivity" in warning for warning in warnings),
                        warnings)

    def test_finfet22_w_cmp_can_stop_on_the_ild_instead_of_a_tuned_height(self):
        # The recorded workaround for run 92f9877d838af76e314eda6e was a cutoff
        # below the ILD top, which also recessed the trench W. Expressing the
        # same intent as "stop on SiO2" must validate against the real fixture.
        project = json.loads(FINFET_PROJECT.read_text(encoding="utf-8"))
        self.assertEqual(project["steps"][10]["cutoffHeight"], 0.092)
        project["steps"][10].pop("cutoffHeight")
        project["steps"][10]["stopMaterial"] = "SiO2"
        project["steps"][10]["overPolish"] = 0.0
        with patch("backend.storage.resolve_gds_asset", return_value=FINFET_GDS):
            parsed, warnings = validate_project(project)
        self.assertEqual(parsed.steps[10].stopMaterial, "SiO2")
        self.assertTrue(warnings)


class PlanarizeDerivationUnit(unittest.TestCase):
    class FakePS:
        def __init__(self):
            self.cutoffs = []

        def Planarize(self, domain, cutoff):
            self.cutoffs.append(cutoff)

            class _Operation:
                def apply(self_inner):
                    return None
            return _Operation()

    def test_effective_cutoff_comes_from_the_measured_stop_top(self):
        ps = self.FakePS()
        step = {"id": "cmp_w_cmp", "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.003}
        # ff22_w_fill in run f6f5481fd1be123be6f90fa6: STI 0.036 and ILD 0.095,
        # median 0.037 (lower plateau is larger), p90 0.095.
        previous_tops = {"SiO2": {"topHeight": 0.09501, "medianTopHeight": 0.03701,
                                  "p90TopHeight": 0.09501, "upFacingArea": 0.4347,
                                  "upFacingCellCount": 1234}}
        actual = apply_geometry_operation(ps, object(), {"dimension": 2}, step, previous_tops=previous_tops)
        self.assertAlmostEqual(ps.cutoffs[0], 0.09201, places=12)
        self.assertAlmostEqual(actual["cutoffHeight"], 0.09201, places=12)
        self.assertEqual(actual["stopMaterial"], "SiO2")
        self.assertEqual(actual["cutoffMode"], "material-aware")
        self.assertEqual(actual["stopHeightMethod"], "area-weighted-p90")
        self.assertEqual(actual["stopHeightPercentile"], 0.9)
        self.assertAlmostEqual(actual["stopHeight"], 0.09501, places=12)
        self.assertAlmostEqual(actual["measuredStopTopHeight"], 0.09501, places=12)
        self.assertAlmostEqual(actual["stopMaterialMedianTopHeight"], 0.03701, places=12)
        self.assertAlmostEqual(actual["overPolish"], 0.003, places=12)

    def test_stop_height_prefers_a_large_upper_plateau(self):
        # STI-like lower plateau with 55% of the area, ILD-like upper plateau
        # with 45%: the median picks the lower one, the p90 must pick the upper.
        tops = measure_material_tops(
            {"points": [[-1.00, 0.036, 0.0], [-0.45, 0.036, 0.0],
                        [0.45, 0.095, 0.0], [0.90, 0.095, 0.0]],
             "lines": [[0, 1], [2, 3]], "cellData": {"MaterialIds": [30, 30]}}, False)
        self.assertAlmostEqual(tops["SiO2"]["upFacingArea"], 1.0, places=12)
        self.assertAlmostEqual(tops["SiO2"]["medianTopHeight"], 0.036, places=12)
        self.assertAlmostEqual(tops["SiO2"]["p90TopHeight"], 0.095, places=12)
        self.assertAlmostEqual(tops["SiO2"]["topHeight"], 0.095, places=12)
        actual = apply_geometry_operation(
            self.FakePS(), object(), {"dimension": 2},
            {"id": "cmp_w_cmp", "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.0},
            previous_tops=tops)
        self.assertAlmostEqual(actual["cutoffHeight"], 0.095, places=12)

    def test_stop_height_ignores_a_narrow_upper_sliver(self):
        # A 4% tall sliver must not move the cut: p90 stays on the field.
        tops = measure_material_tops(
            {"points": [[-1.00, 0.036, 0.0], [-0.10, 0.036, 0.0],
                        [0.10, 0.400, 0.0], [0.14, 0.400, 0.0]],
             "lines": [[0, 1], [2, 3]], "cellData": {"MaterialIds": [30, 30]}}, False)
        area = tops["SiO2"]["upFacingArea"]
        self.assertAlmostEqual(area, 0.94, places=12)
        self.assertAlmostEqual(tops["SiO2"]["topHeight"], 0.400, places=12)
        self.assertAlmostEqual(tops["SiO2"]["medianTopHeight"], 0.036, places=12)
        self.assertAlmostEqual(tops["SiO2"]["p90TopHeight"], 0.036, places=12)
        actual = apply_geometry_operation(
            self.FakePS(), object(), {"dimension": 2},
            {"id": "cmp_w_cmp", "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.0},
            previous_tops=tops)
        self.assertAlmostEqual(actual["cutoffHeight"], 0.036, places=12)

    def test_recorded_surface_without_p90_falls_back_to_the_median(self):
        previous_tops = {"SiO2": {"topHeight": 0.097, "medianTopHeight": 0.095, "upFacingArea": 1.0,
                                  "upFacingCellCount": 12}}
        actual = apply_geometry_operation(
            self.FakePS(), object(), {"dimension": 2},
            {"id": "cmp_w_cmp", "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0.0},
            previous_tops=previous_tops)
        self.assertAlmostEqual(actual["cutoffHeight"], 0.095, places=12)
        self.assertIn("p90 unavailable", actual["stopHeightMethod"])

    def test_missing_stop_surface_fails_with_an_actionable_error(self):
        ps = self.FakePS()
        step = {"id": "cmp_w_cmp", "model": "planarize", "stopMaterial": "SiO2", "overPolish": 0}
        with self.assertRaises(RuntimeError) as ctx:
            apply_geometry_operation(ps, object(), {"dimension": 2}, step, previous_tops={})
        message = str(ctx.exception)
        self.assertIn("SiO2", message)
        self.assertIn("exposed up-facing surface", message)
        self.assertIn("cutoffHeight", message)
        self.assertEqual(ps.cutoffs, [])

    def test_top_measurement_and_change_record_use_the_surface_mesh_only(self):
        # Two up-facing interfaces: SiO2 moves down, a new W surface appears.
        previous = measure_material_tops(
            {"points": [[-1.0, 0.10, 0.0], [1.0, 0.10, 0.0], [-1.0, -1.0, 0.0], [1.0, -1.0, 0.0]],
             "lines": [[0, 1], [2, 3]], "cellData": {"MaterialIds": [30, 10]}}, False)
        self.assertAlmostEqual(previous["SiO2"]["topHeight"], 0.10)
        self.assertAlmostEqual(previous["SiO2"]["medianTopHeight"], 0.10)
        self.assertAlmostEqual(previous["SiO2"]["upFacingArea"], 2.0)

        current = measure_material_tops(
            {"points": [[-1.0, 0.06, 0.0], [1.0, 0.06, 0.0],
                        [-1.0, 0.12, 0.0], [1.0, 0.12, 0.0],
                        [-1.0, -1.0, 0.0], [1.0, -1.0, 0.0]],
             "lines": [[0, 1], [2, 3], [4, 5]], "cellData": {"MaterialIds": [30, 70, 10]}}, False)
        self.assertAlmostEqual(current["SiO2"]["topHeight"], 0.06)
        self.assertAlmostEqual(current["W"]["topHeight"], 0.12)

        removal = measure_step_change({"operation": "etch", "model": "isotropic"}, previous, current, 0.02)
        self.assertEqual(removal["measuredRemoval"]["material"], "SiO2")
        self.assertAlmostEqual(removal["measuredRemoval"]["removedThickness"], 0.04, places=12)
        self.assertAlmostEqual(removal["measuredRemoval"]["topHeightBefore"], 0.10, places=12)

        added = measure_step_change({"operation": "deposit", "model": "isotropic", "depositMaterial": "W"},
                                    previous, current, 0.02)
        self.assertEqual(added["measuredAdded"]["material"], "W")
        # A material that did not exist before grows from the previous top
        # surface (Domain.duplicateTopLevelSet), so the added thickness is the
        # rise above the highest point that existed before the step.
        self.assertAlmostEqual(added["measuredAdded"]["addedThickness"], 0.02, places=12)
        self.assertAlmostEqual(added["measuredAdded"]["topHeightBefore"], 0.10, places=12)

    def test_specialized_ion_beam_records_the_measured_thickness_not_rate_times_duration(self):
        # The 22 nm FinFET spacer etch was configured with planeWaferRate 0.05
        # and 0.2 s (= 10 nm nominal) but removed only about 2 nm. The record
        # must publish the measured number.
        previous = {"Si3N4": {"topHeight": 0.0140, "medianTopHeight": 0.0140, "upFacingArea": 0.803,
                              "upFacingCellCount": 1200},
                    "Si": {"topHeight": -0.0020, "medianTopHeight": -0.0020, "upFacingArea": 0.07,
                           "upFacingCellCount": 90}}
        current = {"Si3N4": {"topHeight": 0.0120, "medianTopHeight": 0.0120, "upFacingArea": 0.066,
                             "upFacingCellCount": 140},
                   "Si": {"topHeight": -0.0020, "medianTopHeight": -0.0020, "upFacingArea": 0.07,
                          "upFacingCellCount": 90}}
        record = measure_step_change({"id": "ff22_spacer_etch", "operation": "etch", "model": "specialized",
                                      "nativeModel": "ionBeam", "duration": 0.2},
                                     previous, current, 0.004)
        removal = record["measuredRemoval"]
        self.assertEqual(removal["material"], "Si3N4")
        self.assertAlmostEqual(removal["removedThickness"], 0.002, places=12)
        self.assertNotAlmostEqual(removal["removedThickness"], 0.05 * 0.2, places=4)
        self.assertAlmostEqual(removal["topHeightBefore"], 0.0140, places=12)
        self.assertAlmostEqual(removal["topHeightAfter"], 0.0120, places=12)
        # An unchanged material is not reported.
        self.assertEqual([row["material"] for row in removal["materials"]], ["Si3N4"])

    def test_changes_below_a_quarter_grid_interval_are_ignored(self):
        previous = {"Si3N4": {"topHeight": 0.0100, "medianTopHeight": 0.0100, "upFacingArea": 1.0,
                              "upFacingCellCount": 10}}
        current = {"Si3N4": {"topHeight": 0.0099, "medianTopHeight": 0.0099, "upFacingArea": 1.0,
                             "upFacingCellCount": 10}}
        self.assertIsNone(measure_step_change({"operation": "etch", "model": "isotropic"}, previous, current, 0.02))

    def test_removal_uses_the_median_exposed_top_not_the_highest_point(self):
        # Real ff22_spacer_etch numbers (run f6f5481fd1be123be6f90fa6): the SiN
        # blanket on the gate tops drops 49.7 nm while the exposed surface that
        # dominates the area only drops 12.4 nm. Reporting the highest-point
        # delta would claim nearly 50 nm was removed.
        previous = {"Si3N4": {"topHeight": 0.10600, "medianTopHeight": 0.04941, "p90TopHeight": 0.0750,
                              "upFacingArea": 0.2566, "upFacingCellCount": 67302},
                    "PolySi": {"topHeight": 0.09600, "medianTopHeight": 0.09600, "p90TopHeight": 0.096,
                               "upFacingArea": 0.0660, "upFacingCellCount": 15583}}
        current = {"Si3N4": {"topHeight": 0.05630, "medianTopHeight": 0.03700, "p90TopHeight": 0.0400,
                             "upFacingArea": 0.0397, "upFacingCellCount": 12000},
                   "PolySi": {"topHeight": 0.09445, "medianTopHeight": 0.09401, "p90TopHeight": 0.0943,
                              "upFacingArea": 0.0627, "upFacingCellCount": 15000}}
        removal = measure_step_change(
            {"id": "ff22_spacer_etch", "operation": "etch", "model": "specialized",
             "nativeModel": "ionBeam", "duration": 1.0}, previous, current, 0.004)["measuredRemoval"]
        self.assertEqual(removal["material"], "Si3N4")
        self.assertAlmostEqual(removal["removedThickness"], 0.01241, places=5)
        self.assertAlmostEqual(removal["medianHeightBefore"], 0.04941, places=5)
        self.assertAlmostEqual(removal["medianHeightAfter"], 0.03700, places=5)
        self.assertAlmostEqual(removal["topHeightBefore"], 0.10600, places=5)
        self.assertAlmostEqual(removal["topHeightAfter"], 0.05630, places=5)
        self.assertAlmostEqual(removal["topHeightDelta"], -0.04970, places=5)
        self.assertEqual(removal["thicknessBasis"], "area-weighted-median")
        self.assertLess(removal["removedThickness"], 0.02)

    def test_median_that_jumps_to_a_lower_patch_is_capped_by_the_highest_change(self):
        # After a CMP the same material can keep only a low liner: its median
        # surface jumps down much further than its topmost surface. The record
        # must not claim that jump as removed thickness.
        previous = {"TiN": {"topHeight": 0.09507, "medianTopHeight": 0.09506, "upFacingArea": 0.2368,
                            "upFacingCellCount": 900},
                    "W": {"topHeight": 0.16507, "medianTopHeight": 0.16506, "upFacingArea": 0.2428,
                          "upFacingCellCount": 900}}
        current = {"TiN": {"topHeight": 0.0920, "medianTopHeight": -0.5500, "upFacingArea": 0.0500,
                           "upFacingCellCount": 90},
                   "W": {"topHeight": 0.0920, "medianTopHeight": 0.0920, "upFacingArea": 0.0900,
                         "upFacingCellCount": 120}}
        removal = measure_step_change({"operation": "etch", "model": "planarize"}, previous, current, 0.004)
        record = removal["measuredRemoval"]
        self.assertEqual(record["material"], "W")
        self.assertAlmostEqual(record["removedThickness"], 0.07306, places=5)
        tin = next(row for row in record["materials"] if row["material"] == "TiN")
        self.assertAlmostEqual(tin["removedThickness"], 0.00307, places=5)
        self.assertIn("highest-top", tin["thicknessBasis"])


class RunHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(2400):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded the CMP acceptance timeout")

    def snapshot(self, run_id: str, step_id: str) -> dict:
        snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(storage.run_path(run_id) / f"{step_id}.json")

    async def run_project(self, project: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)


class MaterialAwareCmpWorkflow(RunHarness):
    async def test_material_aware_cmp_clears_the_field_and_keeps_the_trench_metal(self):
        project = cmp_project()
        run_id, info = await self.run_project(project)
        self.assertEqual(info["stepTotal"], 4)

        before = self.snapshot(run_id, "cmp_w_fill")
        field_w = up_facing_segments(before, "W")
        self.assertGreater(sum(length for _lo, _hi, length in field_w), 3.0,
                           "the deposited W must cover the whole field before the CMP step")

        after = self.snapshot(run_id, "cmp_w_cmp")
        trench_w = up_facing_segments(after, "W")
        self.assertTrue(trench_w, "the trench W must survive the CMP step")
        self.assertLessEqual(sum(length for _lo, _hi, length in trench_w), 1.2)
        self.assertLessEqual(max(high for _lo, high, _length in trench_w), 0.5 + 0.04,
                            "top-facing W must stay inside the 1.0 um trench opening")
        for material in ("W", "TiN"):
            outside = [segment for segment in up_facing_segments(after, material)
                       if segment[0] < -0.52 or segment[1] > 0.52]
            self.assertEqual(outside, [], f"{material} residue is still exposed on the ILD field")

        tops = after["materialTops"]
        self.assertLessEqual(abs(tops["W"]["topHeight"]), 0.02 + 0.01)

        removal = after["measuredRemoval"]
        self.assertEqual(removal["material"], "W")
        self.assertAlmostEqual(removal["topHeightBefore"], 0.55, delta=0.02)
        self.assertAlmostEqual(removal["topHeightAfter"], 0.0, delta=0.02)
        self.assertAlmostEqual(removal["removedThickness"], 0.55, delta=0.02)
        self.assertIn("surface-mesh", removal["method"])
        tin_row = next(row for row in removal["materials"] if row["material"] == "TiN")
        self.assertAlmostEqual(tin_row["removedThickness"], 0.05, delta=0.02)

        added_tin = self.snapshot(run_id, "cmp_tin_barrier")["measuredAdded"]
        self.assertEqual(added_tin["material"], "TiN")
        self.assertAlmostEqual(added_tin["addedThickness"], 0.05, delta=0.02)
        added_w = before["measuredAdded"]
        self.assertEqual(added_w["material"], "W")
        self.assertAlmostEqual(added_w["addedThickness"], 0.5, delta=0.02)

        execution = {row["stepId"]: row for row in info["actualExecution"]}["cmp_w_cmp"]
        parameters = execution["actualParameters"]
        self.assertEqual(parameters["cutoffMode"], "material-aware")
        self.assertEqual(parameters["stopMaterial"], "SiO2")
        self.assertAlmostEqual(parameters["measuredStopTopHeight"], 0.0, delta=0.02)
        self.assertAlmostEqual(parameters["cutoffHeight"], parameters["measuredStopTopHeight"], places=12)

        # The measurement is part of the step metadata the worker writes into
        # the run record (worker-result.json / manifest.workerResult); the
        # filtered manifest.snapshots projection is the supervisor's.
        worker_result = storage.read_json(storage.run_path(run_id) / "worker-result.json")
        metadata = {row["stepId"]: row for row in worker_result["snapshots"]}["cmp_w_cmp"]
        self.assertEqual(metadata["measuredRemoval"]["material"], "W")
        self.assertIn("materialTops", metadata)
        self.assertIn("materialTops", after)

        # A zero-rate step must not inherit the previous step's measurement.
        noop = self.snapshot(run_id, "cmp_zero_rate")
        self.assertTrue(noop["noOp"])
        self.assertNotIn("measuredRemoval", noop)
        self.assertNotIn("measuredAdded", noop)
        self.assertEqual(noop["materialTops"], after["materialTops"])


class MaterialAwareCmpBimodalWorkflow(RunHarness):
    async def test_cmp_stops_on_the_upper_plateau_when_the_lower_one_is_larger(self):
        run_id, info = await self.run_project(cmp_bimodal_project())
        self.assertEqual(info["stepTotal"], 5)

        before = self.snapshot(run_id, "bimodal_w")
        sio2 = before["materialTops"]["SiO2"]
        # Bimodal: the lower plateau (0.0) carries more area than the upper
        # (0.3), so the median is the wrong stop and the p90 is the right one.
        self.assertAlmostEqual(sio2["medianTopHeight"], 0.0, delta=0.02)
        self.assertAlmostEqual(sio2["p90TopHeight"], 0.3, delta=0.02)
        self.assertLess(sio2["medianTopHeight"], 0.1)

        after = self.snapshot(run_id, "bimodal_cmp")
        execution = {row["stepId"]: row for row in info["actualExecution"]}["bimodal_cmp"]
        parameters = execution["actualParameters"]
        self.assertEqual(parameters["stopHeightMethod"], "area-weighted-p90")
        self.assertEqual(parameters["stopHeightPercentile"], 0.9)
        self.assertAlmostEqual(parameters["measuredStopTopHeight"], 0.3, delta=0.02)
        self.assertAlmostEqual(parameters["cutoffHeight"], 0.3 - 0.003, delta=0.02)
        # The old median-based rule would have cut at ~0.0 (0.037 in the real
        # FinFET run); assert the step did not do that.
        self.assertGreater(parameters["cutoffHeight"], 0.25)

        # The overburden above the upper plateau is gone; W stays in the trench
        # below it, so no up-facing W is left outside the 2.4 um opening.
        for material in ("W", "TiN"):
            outside = [segment for segment in up_facing_segments(after, material)
                       if segment[0] < -1.22 or segment[1] > 1.22]
            self.assertEqual(outside, [], f"{material} residue is still exposed on the field")
        self.assertIn("W", after["materialTops"])
        self.assertAlmostEqual(after["materialTops"]["W"]["topHeight"], 0.3 - 0.003, delta=0.02)

        # The lower SiO2 plateau was not the cut target: it is still at ~0.0.
        self.assertAlmostEqual(after["materialTops"]["SiO2"]["medianTopHeight"], 0.0, delta=0.03)


class MaterialAwareCmp3DWorkflow(RunHarness):
    async def test_3d_blanket_overburden_is_removed_down_to_the_stop_material(self):
        run_id, info = await self.run_project(cmp_project_3d())
        self.assertEqual(info["stepTotal"], 3)
        after = self.snapshot(run_id, "step_cmp3d_cmp")

        # Only the stop material is still exposed: the whole TiN/W blanket was
        # cut back to the SiO2 top.
        self.assertEqual(sorted(after["materialTops"]), ["SiO2"])
        self.assertAlmostEqual(after["materialTops"]["SiO2"]["topHeight"], 0.0, delta=0.1)
        self.assertAlmostEqual(after["materialTops"]["SiO2"]["upFacingArea"], 4.0, delta=0.05)

        removal = after["measuredRemoval"]
        self.assertEqual(removal["material"], "W")
        self.assertTrue(removal["removedCompletely"])
        self.assertIsNone(removal["removedThickness"])
        self.assertAlmostEqual(removal["topHeightBefore"], 0.25, delta=0.1)
        self.assertAlmostEqual(removal["remainingTopHeight"], 0.0, delta=0.1)

        added = self.snapshot(run_id, "step_cmp3d_w")["measuredAdded"]
        self.assertEqual(added["material"], "W")
        self.assertAlmostEqual(added["addedThickness"], 0.2, delta=0.1)
