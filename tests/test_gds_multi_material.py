"""Normal workflow acceptance for multi-material GDS imports (2D and 3D).

Every project below uses the shipped worker path (API -> supervisor ->
``worker/simulate.py``).  The GDS fixture has one structure with two layers, so
the top level set must contain both materials; a standalone-body insert would
let Advect clip the lower layer away.
"""

from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import project as base_project

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "gds-mask-diagnosis" / "fixtures" / "multi-layer.gds"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


def workflow_steps(dimension: int) -> list[dict]:
    direction = [0.0, -1.0, 0.0] if dimension == 2 else [0.0, 0.0, -1.0]
    steps = [
        {"id": "step_iso_etch", "name": "等向刻蚀", "enabled": True, "operation": "etch",
         "model": "isotropic", "rateMode": "material", "rateMagnitude": 0.0,
         "materialRates": {"Si": 0.2, "SiO2": 0.1, "PolySi": 0.2, "Si3N4": 0.05},
         "duration": 0.2},
        {"id": "step_dir_etch", "name": "定向刻蚀", "enabled": True, "operation": "etch",
         "model": "directional", "direction": direction,
         "directionalRateMagnitude": 0.1, "isotropicRateMagnitude": 0.05,
         "calculateVisibility": True, "duration": 0.1},
        {"id": "step_deposit_film", "name": "沉积薄膜", "enabled": True, "operation": "deposit",
         "model": "isotropic", "rateMode": "uniform", "rateMagnitude": 0.05,
         "depositMaterial": "W" if dimension == 3 else "Si3N4", "duration": 0.1},
        {"id": "step_planarize", "name": "平坦化", "enabled": True,
         "model": "planarize", "cutoffHeight": 1.2 if dimension == 3 else 0.9},
    ]
    box = {"id": "step_notch", "name": "切槽", "enabled": True, "model": "boolean",
           "booleanOperation": "difference",
           "position": [0.0, 0.0, 1.0] if dimension == 3 else [0.0, 0.6, 0.0],
           "width": 0.6 if dimension == 3 else 0.5,
           "height": 0.6 if dimension == 3 else 0.4,
           "angle": 0.0, "applyToAll": True}
    if dimension == 3:
        box["depth"] = 0.6
    steps.append(box)
    return steps


def gds_project(dimension: int, asset_id: str, layers: list[dict], *,
                boundary: str = "reflective", name: str) -> dict:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = name
    value["domain"] = ({"gridDelta": 0.2, "xExtent": 6.0, "xBoundary": boundary}
                       if dimension == 2 else
                       {"gridDelta": 0.2, "xExtent": 6.0, "yExtent": 4.0, "boundaryXY": boundary})
    value["geometry"] = {
        "kind": "gds", "assetId": asset_id, "fileName": FIXTURE.name,
        "structure": "MULTI", "unitScale": 1.0, "position": [0.0, 0.0, 0.0],
        "layers": copy.deepcopy(layers)}
    value["steps"] = workflow_steps(dimension)
    value["solver"].update({"enginePreference": "cpu", "timeoutSeconds": 90,
                            "raysPerPoint": 32, "random": {"mode": "fixed", "seed": 314159}})
    return value


def material_bounds(snapshot: dict, material: int) -> dict:
    points = snapshot["points"]
    cells = snapshot.get("triangles") or snapshot.get("lines") or []
    ids = snapshot["cellData"]["materialIds"]
    vertices = {vertex for cell, mid in zip(cells, ids) if int(mid) == material for vertex in cell}
    if not vertices:
        return {}
    return {axis: [min(points[index][dimension] for index in vertices),
                   max(points[index][dimension] for index in vertices)]
            for axis, dimension in (("x", 0), ("y", 1), ("z", 2))}


class GdsMultiMaterialWorkflow(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(metadata["structures"][0]["layers"], [1, 2, 99])

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait_run(self, run_id: str) -> dict:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("GDS workflow run did not finish")

    async def execute(self, project: dict) -> tuple[str, dict, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        info = await self.wait_run(run_id)
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        snapshots = []
        for row in manifest["snapshots"]:
            path = snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])
            snapshots.append(storage.read_json(path))
        return run_id, manifest, snapshots

    def assert_all_materials(self, snapshot: dict, materials: set[int], label: str) -> None:
        present = {int(value) for value in snapshot["cellData"]["materialIds"]}
        self.assertTrue(materials <= present,
                        f"{label}: materials {sorted(materials)} missing, present={sorted(present)}")
        self.assertTrue(snapshot["materialIdsByLevelSetIndex"] is not None,
                        f"{label}: missing level-set material map")
        self.assertGreaterEqual(len(snapshot["materialIdsByLevelSetIndex"]), len(materials),
                                f"{label}: too few level sets")

    async def test_2d_two_layers_survive_normal_operations(self):
        project = gds_project(2, self.asset_id,
                              [{"layer": 1, "material": "Si"},
                               {"layer": 2, "material": "SiO2"}],
                              name="GDS 2D two-layer workflow")
        run_id, manifest, snapshots = await self.execute(project)
        initial = snapshots[0]
        self.assertEqual(initial["materialIdsByLevelSetIndex"], [10, 30])
        for snapshot in snapshots:
            step = snapshot["stepId"]
            self.assert_all_materials(snapshot, {10, 30}, step)
            # The lower Si region spans the full polygon, not just the top-layer
            # intersection.  Two grid intervals of tolerance.
            bounds = material_bounds(snapshot, 10)
            self.assertLessEqual(abs(bounds["x"][0] + 1.5), 0.4, step)
            self.assertLessEqual(abs(bounds["x"][1] - 1.5), 0.4, step)
        final = snapshots[-1]
        self.assertIn(16, {int(value) for value in final["cellData"]["materialIds"]})
        self.assertEqual(manifest["childRecord"]["returnCode"], 0)
        self.assertFalse(manifest["childRecord"]["timedOut"])
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance").mkdir(parents=True, exist_ok=True)
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance" / "2d-workflow.json").write_text(
            json.dumps({"runId": run_id, "stepIds": [s["stepId"] for s in snapshots],
                        "materialIds": [sorted({int(v) for v in s["cellData"]["materialIds"]})
                                        for s in snapshots]}, indent=2) + "\n")

    async def test_3d_two_layers_include_native_empty_base_heights(self):
        # baseHeight=0.6/height=0.2 and base=0.2/height=0.4 make the installed
        # 3D layerToLevelSet return an empty body; the product construction must
        # still produce both layers.
        project = gds_project(3, self.asset_id,
                              [{"layer": 1, "material": "PolySi", "baseHeight": 0.6,
                                "height": 0.4, "mask": False},
                               {"layer": 2, "material": "Si3N4", "baseHeight": 0.2,
                                "height": 0.4, "mask": False}],
                              name="GDS 3D two-layer workflow")
        run_id, manifest, snapshots = await self.execute(project)
        initial = snapshots[0]
        self.assertEqual(initial["materialIdsByLevelSetIndex"], [11, 16])
        poly = material_bounds(initial, 11)
        nitride = material_bounds(initial, 16)
        self.assertLessEqual(abs(poly["z"][0] - 0.6), 0.4)
        self.assertLessEqual(abs(poly["z"][1] - 1.0), 0.4)
        self.assertLessEqual(abs(nitride["z"][0] - 0.2), 0.4)
        self.assertLessEqual(abs(nitride["z"][1] - 0.6), 0.4)
        for snapshot in snapshots:
            self.assert_all_materials(snapshot, {11, 16}, snapshot["stepId"])
        self.assertIn(70, {int(value) for value in snapshots[-1]["cellData"]["materialIds"]})
        self.assertEqual(manifest["childRecord"]["returnCode"], 0)
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance").mkdir(parents=True, exist_ok=True)
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance" / "3d-workflow.json").write_text(
            json.dumps({"runId": run_id, "initialMaterials": initial["materialIdsByLevelSetIndex"],
                        "polySiBounds": poly, "si3n4Bounds": nitride},
                       indent=2) + "\n")

    async def test_3d_mask_tone_layer_is_preserved(self):
        project = gds_project(3, self.asset_id,
                              [{"layer": 1, "material": "PolySi", "baseHeight": 0.0,
                                "height": 0.6, "mask": False},
                               {"layer": 2, "material": "Mask", "baseHeight": 0.6,
                                "height": 0.4, "mask": True}],
                              name="GDS 3D mask-tone workflow")
        project["steps"] = [workflow_steps(3)[0]]  # isotropic etch only
        project["steps"][0]["materialRates"] = {"PolySi": 0.2, "Mask": 0.0,
                                                "Si": 0.2, "SiO2": 0.1, "Si3N4": 0.05}
        run_id, manifest, snapshots = await self.execute(project)
        initial, final = snapshots[0], snapshots[-1]
        self.assertEqual(initial["materialIdsByLevelSetIndex"], [11, 0])
        mask_before = material_bounds(initial, 0)
        mask_after = material_bounds(final, 0)
        self.assertTrue(mask_before and mask_after)
        # The negative-tone mask is protected by the step's mask material list.
        self.assertLessEqual(abs(mask_before["z"][1] - mask_after["z"][1]), 0.4)
        self.assert_all_materials(final, {11, 0}, "mask-tone final")
        self.assertEqual(manifest["childRecord"]["returnCode"], 0)
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance").mkdir(parents=True, exist_ok=True)
        (ROOT / "docs" / "gds-mask-diagnosis" / "generic-acceptance" / "3d-mask-tone.json").write_text(
            json.dumps({"runId": run_id, "maskBefore": mask_before, "maskAfter": mask_after},
                       indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
