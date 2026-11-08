"""P0-3 metrology: analytic 2D truth, explicit statuses and historical snapshots."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.workflow.metrology import (
    ALGORITHM_VERSION,
    CDAtHeightParametersV1,
    EtchDepthParametersV1,
    MetrologyRecipeV1,
    MetricDefinitionV1,
    RegionV1,
    measure_snapshot,
    snapshot_content_hash,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_SNAPSHOT = ROOT / "tests" / "fixtures" / "2d-plane-etch.json"


def metric(unit: str = "um", metric_id: str = "metric_cd") -> MetricDefinitionV1:
    return MetricDefinitionV1(
        schemaVersion=1,
        metricId=metric_id,
        name="CD" if metric_id == "metric_cd" else "Depth",
        unit=unit,
    )


def recipe(
    *,
    recipe_id: str = "recipe_cd",
    metric_id: str = "metric_cd",
    version: int = 1,
    method: str = "cd_at_height",
    parameters: dict | None = None,
    material: str = "Si",
    region: RegionV1 | None = None,
    dimension: int = 2,
) -> MetrologyRecipeV1:
    return MetrologyRecipeV1(
        schemaVersion=1,
        recipeId=recipe_id,
        version=version,
        metricId=metric_id,
        name="metrology recipe",
        dimension=dimension,
        method=method,
        parameters=parameters or {"kind": "cd_at_height", "height": -1.0, "pairSelection": "outermost"},
        material=material,
        region=region,
    )


def snapshot(points, lines, materials=None, coordinate_unit: str = "um") -> dict:
    return {
        "dimension": 2,
        "axisMap": {"x": 0, "h": 1, "z": 2},
        "coordinateUnit": coordinate_unit,
        "gridDelta": 0.1,
        "points": [[x, h, 0.0] for x, h in points],
        "lines": lines,
        "cellData": {"materialIds": [10] * len(lines)},
        "materials": materials or {"10": {"name": "Si"}},
    }


def trench() -> dict:
    points = [(-2.0, 0.0), (1.0, 0.0), (1.0, -2.0), (3.0, -2.0), (3.0, 0.0), (5.0, 0.0)]
    lines = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5]]
    return snapshot(points, lines)


def trapezoid() -> dict:
    points = [(-2.0, 0.0), (0.0, 0.0), (2.0, -2.0), (4.0, 0.0), (6.0, 0.0)]
    lines = [[0, 1], [1, 2], [2, 3], [3, 4]]
    return snapshot(points, lines)


def trench_with_outside_edge() -> dict:
    points = [(-2.0, 0.0), (1.0, 0.0), (1.0, -2.0), (3.0, -2.0), (3.0, 0.0), (5.0, 0.0), (10.0, -1.0), (11.0, -1.0)]
    lines = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [6, 7]]
    return snapshot(points, lines)


def double_trench() -> dict:
    points = [
        (-2.0, 0.0), (1.0, 0.0), (1.0, -1.0), (2.0, -1.0), (2.0, 0.0),
        (4.0, 0.0), (4.0, -1.0), (5.0, -1.0), (5.0, 0.0), (7.0, 0.0),
    ]
    lines = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7], [7, 8], [8, 9]]
    return snapshot(points, lines)


class MetrologyAlgorithmTests(unittest.TestCase):
    def test_cd_at_height_on_trench(self):
        result = measure_snapshot(recipe(), metric(), trench())
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 2.0)
        self.assertEqual(result.unit, "um")
        self.assertEqual(result.uncertaintyStatus, "not_evaluated")
        self.assertIsNone(result.uncertainty)
        self.assertEqual(len(result.intersections), 2)

    def test_cd_at_height_on_trapezoid(self):
        result = measure_snapshot(recipe(), metric(), trapezoid())
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 2.0)

    def test_multi_intersection_selection(self):
        outermost = measure_snapshot(
            recipe(parameters={"kind": "cd_at_height", "height": -0.5, "pairSelection": "outermost"}),
            metric(),
            double_trench(),
        )
        first = measure_snapshot(
            recipe(parameters={"kind": "cd_at_height", "height": -0.5, "pairSelection": "first"}),
            metric(),
            double_trench(),
        )
        innermost = measure_snapshot(
            recipe(parameters={"kind": "cd_at_height", "height": -0.5, "pairSelection": "innermost"}),
            metric(),
            double_trench(),
        )
        self.assertEqual(outermost.status, "measured")
        self.assertAlmostEqual(outermost.value, 4.0)
        self.assertAlmostEqual(first.value, 1.0)
        self.assertAlmostEqual(innermost.value, 1.0)

    def test_no_intersection_returns_explicit_status(self):
        result = measure_snapshot(
            recipe(parameters={"kind": "cd_at_height", "height": 1.0}),
            metric(),
            trench(),
        )
        self.assertEqual(result.status, "no_intersection")
        self.assertIsNone(result.value)
        self.assertTrue(result.reason)

    def test_material_not_found_returns_explicit_status(self):
        result = measure_snapshot(recipe(material="SiO2"), metric(), trench())
        self.assertEqual(result.status, "material_not_found")
        self.assertIsNone(result.value)

    def test_region_filters_intersections(self):
        result = measure_snapshot(
            recipe(region=RegionV1(xMin=3.5, xMax=4.5)),
            metric(),
            trench(),
        )
        self.assertEqual(result.status, "region_empty")
        self.assertIsNone(result.value)

    def test_unsupported_coordinate_unit_is_not_silently_converted(self):
        data = trench()
        data["coordinateUnit"] = "nm"
        data["points"] = [[x * 1000.0, h * 1000.0, z] for x, h, z in data["points"]]
        result = measure_snapshot(recipe(), metric(), data)
        self.assertEqual(result.status, "unsupported_units")
        self.assertIsNone(result.value)
        self.assertTrue(result.reason)

    def test_horizontal_edge_outside_region_does_not_make_cd_ambiguous(self):
        result = measure_snapshot(
            recipe(region=RegionV1(xMin=-2.5, xMax=6.5)),
            metric(),
            trench_with_outside_edge(),
        )
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 2.0)

    def test_etch_depth_on_trench(self):
        result = measure_snapshot(
            recipe(
                recipe_id="recipe_depth",
                metric_id="metric_depth",
                method="etch_depth",
                parameters={"kind": "etch_depth", "x": 2.0, "referenceHeight": 0.0},
            ),
            metric(metric_id="metric_depth"),
            trench(),
        )
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 2.0)

    def test_unit_conversion_to_nanometres(self):
        result = measure_snapshot(recipe(), metric(unit="nm"), trench())
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 2000.0)
        self.assertEqual(result.unit, "nm")

    def test_3d_material_top_depth(self):
        snapshot_3d = {
            "dimension": 3,
            "axisMap": {"x": 0, "y": 1, "z": 2},
            "coordinateUnit": "um",
            "gridDelta": 0.1,
            "points": [],
            "lines": [],
            "cellData": {"materialIds": []},
            "materials": {"10": {"name": "Si"}},
            "materialTops": {"Si": {"medianTopHeight": 0.04, "topHeight": 0.05}},
        }
        result = measure_snapshot(
            recipe(
                recipe_id="recipe_3d",
                metric_id="metric_3d",
                dimension=3,
                method="etch_depth",
                parameters={"kind": "etch_depth", "x": 0, "referenceHeight": 0.1},
            ),
            metric(metric_id="metric_3d"),
            snapshot_3d,
        )
        self.assertEqual(result.status, "measured")
        self.assertAlmostEqual(result.value, 0.06)

    def test_real_historical_snapshot_is_content_hashed(self):
        snapshot_data = json.loads(REAL_SNAPSHOT.read_text(encoding="utf-8"))
        depth_recipe = recipe(
            recipe_id="recipe_real_depth",
            metric_id="metric_real_depth",
            method="etch_depth",
            parameters={"kind": "etch_depth", "x": 0.0, "referenceHeight": 0.0},
        )
        first = measure_snapshot(
            depth_recipe,
            metric(metric_id="metric_real_depth"),
            snapshot_data,
            source_run_id=snapshot_data["runId"],
            source_step_id=snapshot_data["stepId"],
        )
        second = measure_snapshot(
            depth_recipe,
            metric(metric_id="metric_real_depth"),
            snapshot_data,
            source_run_id=snapshot_data["runId"],
            source_step_id=snapshot_data["stepId"],
        )
        self.assertEqual(first.status, "measured")
        self.assertGreater(first.value, 0)
        self.assertEqual(first.sourceSnapshotHash, snapshot_content_hash(snapshot_data))
        self.assertEqual(first.sourceSnapshotHash, second.sourceSnapshotHash)
        self.assertNotEqual(first.measurementId, second.measurementId)
        self.assertEqual(first.algorithmVersion, ALGORITHM_VERSION)


if __name__ == "__main__":
    unittest.main()
