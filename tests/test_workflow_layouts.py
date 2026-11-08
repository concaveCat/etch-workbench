"""P2 layout extraction, clip assets and layout-to-domain compiles."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("WORKBENCH_PORT", "8767")

import asyncio  # noqa: E402

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import ProjectV7  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_adoption import project  # noqa: E402


def project_3d() -> dict:
    """A 3D project with parametric geometry; compile_clip replaces geometry."""
    value = project()
    value["name"] = "adoption 3D"
    value["dimension"] = 3
    value["domain"] = {"gridDelta": 0.2, "xExtent": 4.0, "yExtent": 4.0, "boundaryXY": "reflective"}
    value["geometry"] = {
        "kind": "hole", "radius": 1.0, "depth": 2.0, "taperAngle": 0.0,
        "maskHeight": 0.0, "maskTaperAngle": 0.0,
        "substrateMaterial": "Si", "maskMaterial": "Mask",
    }
    value.pop("view", None)
    value["view3D"] = {
        "projection": "parallel", "camera": None, "displayMode": "surface",
        "visibleMaterialIds": [], "opacityByMaterial": {},
        "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
        "section": {"enabled": False, "axis": "y", "position": 0.0},
    }
    return value


def _without_none(value):
    """Response models keep optional keys as null; ProjectV7 rejects that."""
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


class LayoutClipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        source = Path(__file__).resolve().parents[1] / "docs" / "stage5-evidence" / "two-structure-two-layer.gds"
        uploaded = await asgi_request(
            "/api/gds-assets?file_name=two-structure-two-layer.gds",
            method="POST", payload=source.read_bytes(),
            headers=[(b"content-type", b"application/octet-stream")],
        )
        self.assertEqual(uploaded[0], 201, uploaded[2])
        self.asset_id = json_body(uploaded)["assetId"]
        registered = await asgi_request(
            "/api/v1/workflow/layouts", method="POST",
            payload={"assetId": self.asset_id, "fileName": "two-structure-two-layer.gds"},
        )
        self.assertEqual(registered[0], 201, registered[2])
        self.layout = json_body(registered)

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def _clip(self, name: str, structure: str, layer: int, bbox: dict, annotations=None) -> dict:
        response = await asgi_request(
            "/api/v1/workflow/clips", method="POST",
            payload={
                "layoutId": self.layout["layoutId"], "name": name, "structure": structure,
                "layers": [{"layer": layer, "material": "Si"}], "bbox": bbox,
                "annotations": annotations or [],
            },
        )
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def _wait_experiment(self, experiment_id: str, timeout: float = 60.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        document = {}
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/v1/workflow/experiments/{experiment_id}")
            document = json_body(response)
            if document.get("status") in {"completed", "failed"}:
                return document
            await asyncio.sleep(0.3)
        self.fail(f"experiment did not finish: {document.get('status')}")

    async def test_clip_compile_binding_is_reused_by_experiment_runs(self):
        import uuid

        clip = await self._clip(
            "binding clip", "CELL_A", 1,
            {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1},
        )
        compiled = await asgi_request(
            f"/api/v1/workflow/clips/{clip['clipId']}/compile", method="POST",
            payload={"project": project(), "flowId": "flow_clip_experiment"},
        )
        self.assertEqual(compiled[0], 201, compiled[2])
        compiled_body = json_body(compiled)
        binding = compiled_body["binding"]
        suffix = uuid.uuid4().hex[:6]
        metric_id = f"metric_clip_{suffix}"
        recipe_id = f"recipe_clip_{suffix}"
        for path, payload in (
            ("/api/v1/workflow/metrics", {"schemaVersion": 1, "metricId": metric_id, "name": "depth", "unit": "um"}),
            ("/api/v1/workflow/metrology-recipes", {
                "schemaVersion": 1, "recipeId": recipe_id, "version": 1, "metricId": metric_id,
                "name": "depth", "dimension": 2, "method": "etch_depth",
                "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0}, "material": "Si",
            }),
        ):
            await asgi_request(path, method="POST", payload=payload)
        created = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={
                "name": "clip experiment", "project": project(), "flowId": "flow_clip_experiment",
                "compileId": compiled_body["compileId"], "recipeId": recipe_id, "recipeVersion": 1,
                "variants": [{"parameters": {}, "replicates": 1, "seed": 91}],
            },
        )
        self.assertEqual(created[0], 201, created[2])
        document = json_body(created)
        self.assertEqual(document["bindingId"], binding["bindingId"])
        self.assertEqual(document["binding"]["geometrySource"]["clipId"], clip["clipId"])
        started = await asgi_request(
            f"/api/v1/workflow/experiments/{document['experimentId']}/run", method="POST", payload={}
        )
        self.assertEqual(started[0], 200, started[2])
        final = await self._wait_experiment(document["experimentId"])
        self.assertEqual(final["status"], "completed", final)
        replicate = final["variants"][0]["replicates"][0]
        self.assertEqual(replicate["status"], "succeeded", replicate)
        self.assertTrue(replicate["statisticalSample"])
        manifest = storage.read_json(storage.run_path(replicate["runId"]) / "manifest.json")
        self.assertEqual(manifest["normalizedProject"]["geometry"]["kind"], "gds")
        self.assertEqual(manifest["normalizedProject"]["geometry"]["assetId"], clip["gdsAssetId"])
        self.assertEqual(manifest["normalizedProject"]["domain"]["xExtent"], binding["domain"]["xExtent"])
        route = json_body(await asgi_request(f"/api/v1/workflow/routes/{replicate['routeId']}"))["plan"]
        segment = next(item for item in route["segments"] if item["segmentId"] == replicate["segmentId"])
        run_compile = json_body(await asgi_request(f"/api/v1/workflow/compiles/{segment['sourceCompileId']}"))
        self.assertEqual(run_compile["compileRecord"]["geometryBindingId"], binding["bindingId"])
        self.assertEqual(run_compile["compileRecord"]["geometryBindingHash"], compiled_body["compile"]["compileRecord"]["geometryBindingHash"])
        self.assertEqual(run_compile["project"]["geometry"]["kind"], "gds")
        self.assertEqual(run_compile["project"]["geometry"]["assetId"], clip["gdsAssetId"])

    async def test_annotation_change_creates_new_binding_and_old_binding_remains(self):
        clip = await self._clip(
            "binding annotation clip", "CELL_A", 1,
            {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1},
        )
        first = await asgi_request(
            f"/api/v1/workflow/clips/{clip['clipId']}/compile", method="POST",
            payload={"project": project(), "flowId": "flow_clip_annotation"},
        )
        self.assertEqual(first[0], 201, first[2])
        first_body = json_body(first)
        annotated = await asgi_request(
            f"/api/v1/workflow/clips/{clip['clipId']}/annotations", method="POST",
            payload={"text": "new annotation", "x": 0.5, "y": 0.2},
        )
        self.assertEqual(annotated[0], 201, annotated[2])
        second = await asgi_request(
            f"/api/v1/workflow/clips/{clip['clipId']}/compile", method="POST",
            payload={"project": project(), "flowId": "flow_clip_annotation"},
        )
        self.assertEqual(second[0], 201, second[2])
        second_body = json_body(second)
        self.assertNotEqual(first_body["binding"]["bindingId"], second_body["binding"]["bindingId"])
        self.assertNotEqual(
            first_body["compile"]["compileRecord"]["geometryBindingHash"],
            second_body["compile"]["compileRecord"]["geometryBindingHash"],
        )
        old = await asgi_request(
            f"/api/v1/workflow/geometry-bindings/{first_body['binding']['bindingId']}"
        )
        self.assertEqual(old[0], 200, old[2])
        self.assertEqual(json_body(old)["bindingId"], first_body["binding"]["bindingId"])
        self.assertEqual(json_body(old)["geometrySource"]["clipHash"], first_body["binding"]["geometrySource"]["clipHash"])

    async def test_hierarchical_reference_flattens_into_layout_bounds(self):
        source = Path(__file__).resolve().parents[1] / "docs" / "stage5-evidence" / "hierarchical-reference.gds"
        uploaded = await asgi_request(
            "/api/gds-assets?file_name=hierarchical-reference.gds",
            method="POST", payload=source.read_bytes(),
            headers=[(b"content-type", b"application/octet-stream")],
        )
        self.assertEqual(uploaded[0], 201, uploaded[2])
        asset_id = json_body(uploaded)["assetId"]
        registered = await asgi_request(
            "/api/v1/workflow/layouts", method="POST",
            payload={"assetId": asset_id, "fileName": "hierarchical-reference.gds"},
        )
        self.assertEqual(registered[0], 201, registered[2])
        layout = json_body(registered)
        top = next(item for item in layout["structures"] if item["name"] == "TOP")
        self.assertEqual(top["bounds"], {"x0": 3.0, "y0": 0.0, "x1": 5.0, "y1": 1.0})
        preview = await asgi_request(
            f"/api/v1/workflow/layouts/{layout['layoutId']}/preview", method="POST",
            payload={
                "structure": "TOP", "layers": [{"layer": 3, "material": "Si"}],
                "bbox": {"x0": 2.0, "y0": -1.0, "x1": 6.0, "y1": 2.0},
            },
        )
        self.assertEqual(preview[0], 200, preview[2])
        body = json_body(preview)
        self.assertEqual(body["polygonCount"], 1)
        self.assertEqual(body["contentBounds"], {"x0": 1.0, "y0": 1.0, "x1": 3.0, "y1": 2.0})

    async def test_extract_three_clips_and_compile_same_flow_three_times(self):
        preview = await asgi_request(
            f"/api/v1/workflow/layouts/{self.layout['layoutId']}/preview", method="POST",
            payload={
                "structure": "CELL_A",
                "layers": [{"layer": 1, "material": "Si"}],
                "bbox": {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1},
            },
        )
        self.assertEqual(preview[0], 200, preview[2])
        preview_body = json_body(preview)
        self.assertEqual(preview_body["polygonCount"], 1)
        self.assertEqual(preview_body["polygons"][0]["layer"], 1)

        clip_a = await self._clip(
            "clip A", "CELL_A", 1,
            {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1},
        )
        clip_b = await self._clip(
            "clip B", "CELL_B", 2,
            {"x0": 2.9, "y0": -0.1, "x1": 5.1, "y1": 2.1},
        )
        clip_c = await self._clip(
            "clip C", "CELL_A", 1,
            {"x0": 0.5, "y0": 0.2, "x1": 1.5, "y1": 0.8},
            annotations=[{"text": "center", "x": 0.5, "y": 0.3}],
        )
        for clip in (clip_a, clip_b, clip_c):
            self.assertRegex(clip["gdsAssetId"], r"^[a-f0-9]{64}$")
            self.assertRegex(clip["clipHash"], r"^[a-f0-9]{64}$")
            self.assertGreaterEqual(clip["polygonCount"], 1)
            self.assertTrue(storage.resolve_gds_asset(clip["gdsAssetId"]).is_file())

        annotated = await asgi_request(
            f"/api/v1/workflow/clips/{clip_c['clipId']}/annotations", method="POST",
            payload={"text": "edge", "x": 1.0, "y": 0.1},
        )
        self.assertEqual(annotated[0], 201, annotated[2])
        clip_c = json_body(annotated)
        self.assertEqual(len(clip_c["annotations"]), 2)

        compiled = []
        flow_id = "flow_clip_p2"
        for clip in (clip_a, clip_b, clip_c):
            response = await asgi_request(
                f"/api/v1/workflow/clips/{clip['clipId']}/compile", method="POST",
                payload={"project": project(), "flowId": flow_id},
            )
            self.assertEqual(response[0], 201, response[2])
            body = json_body(response)
            self.assertEqual(body["dimension"], 2)
            self.assertEqual(body["binding"]["geometrySource"]["clipId"], clip["clipId"])
            self.assertEqual(body["binding"]["geometrySource"]["clipHash"], clip["clipHash"])
            self.assertRegex(body["binding"]["bindingId"], r"^bind_[a-f0-9]{16}$")
            compiled.append(body)

        hashes = {body["compile"]["compileRecord"]["geometryBindingHash"] for body in compiled}
        self.assertEqual(len(hashes), 3)
        self.assertEqual(len({body["binding"]["bindingId"] for body in compiled}), 3)
        step_ids = {body["compile"]["project"]["steps"][0]["id"] for body in compiled}
        self.assertEqual(len(step_ids), 1, "same flow must keep stable compiled step ids across clips")
        authored = {body["compile"]["sourceMap"][0]["authoredStepId"] for body in compiled}
        self.assertEqual(authored, {"step_etch_01"})
        self.assertTrue(all(body["compile"]["project"]["geometry"]["kind"] == "gds" for body in compiled))

        listing = await asgi_request(f"/api/v1/workflow/clips?layout_id={self.layout['layoutId']}")
        self.assertEqual(listing[0], 200, listing[2])
        listed_ids = {clip["clipId"] for clip in json_body(listing)["clips"]}
        self.assertTrue({clip_a["clipId"], clip_b["clipId"], clip_c["clipId"]} <= listed_ids)


    async def _clip_layers(self, name: str, structure: str, layers: list[dict], bbox: dict) -> dict:
        response = await asgi_request(
            "/api/v1/workflow/clips", method="POST",
            payload={
                "layoutId": self.layout["layoutId"], "name": name, "structure": structure,
                "layers": layers, "bbox": bbox, "annotations": [],
            },
        )
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def test_3d_clip_roundtrip_hash_and_compile(self):
        bbox = {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1}
        clip = await self._clip_layers(
            "3d clip", "CELL_A",
            [{"layer": 1, "material": "Si", "baseHeight": 0.0, "height": 0.6}], bbox,
        )
        self.assertEqual(clip["layers"][0]["baseHeight"], 0.0)
        self.assertEqual(clip["layers"][0]["height"], 0.6)
        fetched = json_body(await asgi_request(f"/api/v1/workflow/clips/{clip['clipId']}"))
        self.assertEqual(fetched["layers"][0]["height"], 0.6)
        self.assertEqual(fetched["clipHash"], clip["clipHash"])

        # The layer height is part of the clip hash: same polygons, new stack.
        taller = await self._clip_layers(
            "3d clip taller", "CELL_A",
            [{"layer": 1, "material": "Si", "baseHeight": 0.0, "height": 0.8}], bbox,
        )
        self.assertNotEqual(clip["clipHash"], taller["clipHash"])

        compiled = await asgi_request(
            f"/api/v1/workflow/clips/{clip['clipId']}/compile", method="POST",
            payload={"project": project_3d(), "flowId": "flow_clip_3d"},
        )
        self.assertEqual(compiled[0], 201, compiled[2])
        body = json_body(compiled)
        self.assertEqual(body["dimension"], 3)
        self.assertEqual(body["binding"]["dimension"], 3)
        self.assertEqual(body["binding"]["geometrySource"]["clipId"], clip["clipId"])
        project_body = body["compile"]["project"]
        ProjectV7.model_validate(_without_none(project_body))
        self.assertEqual(project_body["geometry"]["kind"], "gds")
        self.assertEqual(project_body["geometry"]["assetId"], clip["gdsAssetId"])
        self.assertEqual(
            project_body["geometry"]["layers"],
            [{"layer": 1, "material": "Si", "baseHeight": 0.0, "height": 0.6}],
        )
        grid = 0.2
        self.assertEqual(
            project_body["domain"],
            {
                "gridDelta": grid,
                "xExtent": max(clip["width"] + 2 * grid, 4 * grid),
                "yExtent": max(clip["height"] + 2 * grid, 4 * grid),
                "boundaryXY": "reflective",
            },
        )

    async def test_3d_clip_compile_rejects_missing_and_thin_layers(self):
        bbox = {"x0": -0.1, "y0": -0.1, "x1": 2.1, "y1": 1.1}
        flat = await self._clip("3d clip without heights", "CELL_A", 1, bbox)
        self.assertIsNone(flat["layers"][0]["baseHeight"])
        self.assertIsNone(flat["layers"][0]["height"])
        missing = await asgi_request(
            f"/api/v1/workflow/clips/{flat['clipId']}/compile", method="POST",
            payload={"project": project_3d(), "flowId": "flow_clip_3d_missing"},
        )
        self.assertEqual(missing[0], 422, missing[2])
        self.assertIn("baseHeight", json_body(missing)["detail"])

        thin = await self._clip_layers(
            "3d clip too thin", "CELL_A",
            [{"layer": 1, "material": "Si", "baseHeight": 0.0, "height": 0.1}], bbox,
        )
        rejected = await asgi_request(
            f"/api/v1/workflow/clips/{thin['clipId']}/compile", method="POST",
            payload={"project": project_3d(), "flowId": "flow_clip_3d_thin"},
        )
        self.assertEqual(rejected[0], 422, rejected[2])
        self.assertIn("厚度", json_body(rejected)["detail"])

        # A 2D compile of the same height-less clip still succeeds unchanged.
        two_d = await asgi_request(
            f"/api/v1/workflow/clips/{flat['clipId']}/compile", method="POST",
            payload={"project": project(), "flowId": "flow_clip_2d_regression"},
        )
        self.assertEqual(two_d[0], 201, two_d[2])
        self.assertEqual(json_body(two_d)["dimension"], 2)


if __name__ == "__main__":
    unittest.main()
