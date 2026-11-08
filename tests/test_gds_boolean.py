"""Schema-7 GDS-mask boolean step: contract, hash and native acceptance.

The step re-reads the project's own GDS asset and extrudes one selected
layer/datatype into a real 3D operand for the native
``Domain.applyBooleanOperation``. These tests prove the contract is strict
(schema 7, 3D, GDS geometry only), that the request hash only changes when the
step is present, and that a ``difference`` mask really removes material inside
the polygon while leaving it untouched outside.

The static fixture ``multi-layer.gds`` already ships with a closed mask layer
(``MULTI``: layer 1 = 3.0 x 1.0 um rectangle, layer 2 = 1.0 x 1.6 um rectangle),
so no gdstk dependency is needed.
"""

from __future__ import annotations

import asyncio
import copy
import unittest
from pathlib import Path

from backend.hashing import requested_input, requested_input_hash
from backend.main import app
from backend.models import BooleanStepV5, GdsBooleanStepV7, parse_project
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import project as base_project

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "gds-mask-diagnosis" / "fixtures" / "multi-layer.gds"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
MATERIAL_SI = 10


def mask_step(*, step_id: str = "step_gds_cut", operation: str = "difference",
              layer: int = 2, datatype: int = 0, base_height: float = 0.0,
              height: float = 0.6, position: list[float] | None = None,
              retained: str = "inside", apply_to_all: bool = True) -> dict:
    return {
        "id": step_id, "name": "GDS 接触孔", "enabled": True, "model": "gdsBoolean",
        "booleanOperation": operation, "applyToAll": apply_to_all,
        "gdsMask": {
            "layer": layer, "datatype": datatype, "unitScale": 1.0,
            "position": position if position is not None else [0.0, 0.0, 0.0],
            "baseHeight": base_height, "height": height, "retainedRegion": retained,
        },
    }


def gds_project(*, asset_id: str = "a" * 64, dimension: int = 3,
                steps: list[dict] | None = None, grid_delta: float = 0.1) -> dict:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = "GDS 布尔接触孔"
    value["domain"] = ({"gridDelta": grid_delta, "xExtent": 4.0, "xBoundary": "reflective"}
                       if dimension == 2 else
                       {"gridDelta": grid_delta, "xExtent": 4.0, "yExtent": 4.0,
                        "boundaryXY": "reflective"})
    value["geometry"] = {
        "kind": "gds", "assetId": asset_id, "fileName": FIXTURE.name,
        "structure": "MULTI", "unitScale": 1.0, "position": [0.0, 0.0, 0.0],
        # The -0.6 base keeps a substrate under the 0.6 um mask so the cut is a
        # recess/hole rather than an empty through-slot.
        "layers": [{"layer": 1, "material": "Si", "baseHeight": -0.6,
                    "height": 1.2, "mask": False}],
    }
    value["steps"] = steps if steps is not None else [mask_step()]
    value["outputSettings"] = {"levelSets": False, "levelSetMeshes": False,
                               "surfaceMeshVtk": False, "volumeMesh": False,
                               "nativeMetadata": False}
    return value


def silicon_top_at(snapshot: dict, x: float, y: float) -> float | None:
    """Highest Si triangle crossing (x, y); the mask cut lowers it inside a hole."""
    points = snapshot["points"]
    cells = snapshot.get("triangles") or []
    ids = snapshot["cellData"]["materialIds"]
    best: float | None = None
    for cell, material in zip(cells, ids):
        if int(material) != MATERIAL_SI:
            continue
        a, b, c = (points[index] for index in cell[:3])
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-12:
            continue
        u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
        v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
        if min(u, v, 1.0 - u - v) < -1e-8:
            continue
        z = u * a[2] + v * b[2] + (1.0 - u - v) * c[2]
        best = z if best is None else max(best, z)
    return best


class GdsBooleanContract(unittest.TestCase):
    """Model-level rules; no asset or native process is needed here."""

    def test_only_3d_gds_projects_accept_the_step(self):
        parsed = parse_project(gds_project())
        step = parsed.steps[0]
        self.assertIsInstance(step, GdsBooleanStepV7)
        self.assertEqual(step.gdsMask.datatype, 0)
        self.assertEqual(step.gdsMask.retainedRegion, "inside")
        self.assertIs(step.applyToAll, True)

        two_d = gds_project(dimension=2)
        with self.assertRaises(Exception) as caught:
            parse_project(two_d)
        self.assertIn("3D", str(caught.exception))

        plane = gds_project()
        plane["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
        with self.assertRaises(Exception) as caught:
            parse_project(plane)
        self.assertIn("geometry.kind=gds", str(caught.exception))

        old_schema = gds_project()
        old_schema["schemaVersion"] = 6
        with self.assertRaises(Exception):
            parse_project(old_schema)

    def test_mask_height_and_budget_are_enforced(self):
        too_thin = gds_project(steps=[mask_step(height=0.15)])
        with self.assertRaises(Exception) as caught:
            parse_project(too_thin)
        self.assertIn("two grid intervals", str(caught.exception))

        budget = gds_project(steps=[mask_step(base_height=2.0e5, height=0.6)])
        with self.assertRaises(Exception) as caught:
            parse_project(budget)
        self.assertIn("coordinate budget", str(caught.exception))

        far = gds_project(steps=[mask_step(position=[2.0e5, 0.0, 0.0])])
        with self.assertRaises(Exception) as caught:
            parse_project(far)
        self.assertIn("coordinate budget", str(caught.exception))

    def test_absent_mask_is_rejected(self):
        missing = gds_project()
        del missing["steps"][0]["gdsMask"]
        with self.assertRaises(Exception):
            parse_project(missing)

        bad_field = gds_project()
        bad_field["steps"][0]["gdsMask"]["retainedRegion"] = "ring"
        with self.assertRaises(Exception):
            parse_project(bad_field)

    def test_hash_only_changes_when_the_step_is_present_and_configured(self):
        parsed = parse_project(gds_project())
        baseline = requested_input_hash(parsed)
        input_steps = requested_input(parsed)["steps"]
        self.assertEqual(input_steps[0]["model"], "gdsBoolean")
        self.assertEqual(input_steps[0]["gdsMask"]["layer"], 2)
        # Omitted defaults normalize to explicit defaults: same request hash.
        explicit = gds_project()
        explicit["steps"][0]["gdsMask"].update({"datatype": 0, "unitScale": 1.0,
                                                "retainedRegion": "inside"})
        self.assertEqual(baseline, requested_input_hash(parse_project(explicit)))

        other_layer = copy.deepcopy(gds_project())
        other_layer["steps"][0]["gdsMask"]["layer"] = 1
        self.assertNotEqual(baseline, requested_input_hash(parse_project(other_layer)))
        other_height = copy.deepcopy(gds_project())
        other_height["steps"][0]["gdsMask"]["height"] = 0.4
        self.assertNotEqual(baseline, requested_input_hash(parse_project(other_height)))

        # A schema-7 project without a GDS boolean step keeps its old shape: the
        # step model carries no new keys and the box step remains untouched.
        box_only = gds_project(steps=[{"id": "step_box_cut", "name": "盒体切割",
                                       "enabled": True, "model": "boolean",
                                       "booleanOperation": "difference",
                                       "position": [0.0, 0.0, 0.0], "width": 1.0,
                                       "height": 0.6, "depth": 1.0, "angle": 0.0,
                                       "applyToAll": True}])
        parsed_box = parse_project(box_only)
        self.assertIsInstance(parsed_box.steps[0], BooleanStepV5)
        keys = set(parsed_box.steps[0].model_dump(mode="json", exclude_none=True))
        self.assertNotIn("gdsMask", keys)
        self.assertEqual(requested_input(parsed_box)["steps"][0]["model"], "boolean")


class GdsBooleanWorkflow(unittest.IsolatedAsyncioTestCase):
    """The shipped worker path: API -> supervisor -> worker/simulate.py."""

    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()
        response = await asgi_request(
            "/api/gds-assets?file_name=multi-layer.gds", method="POST",
            payload=FIXTURE.read_bytes(),
            headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        metadata = json_body(response)
        self.asset_id = metadata["assetId"]
        self.assertEqual(metadata["structures"][0]["name"], "MULTI")

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait_run(self, run_id: str) -> dict:
        for _ in range(1800):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                return info
            await asyncio.sleep(0.05)
        self.fail("GDS boolean run did not finish")

    async def run_project(self, project: dict) -> tuple[dict, list[dict]]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        info = await self.wait_run(run_id)
        self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
        from backend import snapshots as snapshot_store, storage
        snapshots = []
        for row in info["snapshots"]:
            snapshots.append(storage.read_json(
                snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])))
        return info, snapshots

    async def test_layer_and_structure_must_exist_in_the_asset(self):
        unknown_layer = gds_project(asset_id=self.asset_id, steps=[mask_step(layer=7)])
        with self.assertRaises(ProjectInputError) as caught:
            validate_project(unknown_layer)
        issues = {issue.path: issue.message for issue in caught.exception.issues}
        self.assertIn("steps[0].gdsMask", issues)
        self.assertIn("not present", issues["steps[0].gdsMask"])

        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=unknown_layer)
        self.assertEqual(response[0], 422, response[2])

    async def test_difference_mask_cuts_the_expected_hole(self):
        info, snapshots = await self.run_project(gds_project(asset_id=self.asset_id))
        initial, final = snapshots[0], snapshots[-1]
        # Before the step the Si slab is flat at z=0.6 everywhere.
        self.assertAlmostEqual(silicon_top_at(initial, 0.0, 0.0), 0.6, places=6)
        self.assertAlmostEqual(silicon_top_at(initial, 1.0, 0.0), 0.6, places=6)
        # Inside the layer-2 rectangle (x in [-0.5,0.5], y in [-0.5,0.5]) the
        # mask body removes the whole 0.6 um upper part: the hole floor is z=0.
        self.assertAlmostEqual(silicon_top_at(final, 0.0, 0.0), 0.0, places=6)
        # Outside the polygon nothing changed.
        self.assertAlmostEqual(silicon_top_at(final, 1.0, 0.0), 0.6, places=6)
        self.assertAlmostEqual(silicon_top_at(final, -1.4, 0.4), 0.6, places=6)
        # The hole edge stays within two grid intervals of the polygon boundary.
        self.assertGreater(silicon_top_at(final, 0.7, 0.0), 0.5)
        self.assertLess(silicon_top_at(final, 0.3, 0.0), 0.1)
        # No level set vanished: the cut is a real recess, not a destroyed body.
        self.assertEqual(len(final["materialIdsByLevelSetIndex"]), 1)
        self.assertIn(MATERIAL_SI, {int(value) for value in final["cellData"]["materialIds"]})

        execution = {row["stepId"]: row for row in info["actualExecution"]}
        actual = execution["step_gds_cut"]["actualParameters"]
        self.assertEqual(actual["geometryOperation"], "gdsBoolean")
        self.assertEqual(actual["operand"], "gds-mask")
        self.assertEqual(actual["nativeBooleanEnum"], "RELATIVE_COMPLEMENT")
        self.assertEqual(actual["mask"]["layer"], 2)
        self.assertEqual(actual["mask"]["datatype"], 0)
        self.assertEqual(actual["mask"]["baseHeight"], 0.0)
        self.assertEqual(actual["mask"]["height"], 0.6)
        self.assertEqual(actual["mask"]["retainedRegion"], "inside")
        self.assertIs(actual["applyToAll"], True)
        diagnostics = actual["maskDiagnostics"]
        self.assertEqual(diagnostics["gridSource"], "simulation-domain")
        self.assertEqual(diagnostics["selectedStructure"], "MULTI")
        self.assertEqual(diagnostics["selectedLayerDatatypes"], [[2, 0]])
        self.assertGreater(diagnostics["alignedMaskPointCount"], 0)
        self.assertGreater(diagnostics["planarPointCount"], 0)
        self.assertEqual(diagnostics["filteredFile"], "gds-boolean-step_gds_cut-selected.gds")

    async def test_project_persists_and_keeps_its_hash(self):
        project = gds_project(asset_id=self.asset_id)
        parsed, _ = validate_project(project)
        response = await asgi_request("/api/projects", method="POST", payload=project)
        self.assertEqual(response[0], 201, response[2])
        project_id = json_body(response)["id"]
        reopened = json_body(await asgi_request(f"/api/projects/{project_id}"))["project"]
        self.assertEqual(reopened, parsed.model_dump(mode="json", exclude_none=True))
        step = reopened["steps"][0]
        self.assertEqual(step["model"], "gdsBoolean")
        self.assertEqual(step["gdsMask"], {"layer": 2, "datatype": 0, "unitScale": 1.0,
                                           "position": [0.0, 0.0, 0.0], "baseHeight": 0.0,
                                           "height": 0.6, "retainedRegion": "inside"})
        self.assertEqual(requested_input_hash(parse_project(project)),
                         requested_input_hash(parse_project(reopened)))

    async def test_empty_mask_position_fails_instead_of_silently_doing_nothing(self):
        # The polygons land far outside the 4 x 4 um domain, so the regridded
        # mask is empty. The run must fail with the explicit mask error rather
        # than completing as a no-op.
        response = await asgi_request(
            "/api/runs", method="POST",
            payload={"kind": "simulate",
                     "project": gds_project(asset_id=self.asset_id,
                                            steps=[mask_step(position=[50.0, 50.0, 0.0])])})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        info = await self.wait_run(run_id)
        self.assertEqual(info["status"], "failed", info)
        reason = info.get("failureReason") or ""
        self.assertIn("GDS 掩膜在当前计算域内为空", reason)


if __name__ == "__main__":
    unittest.main()
