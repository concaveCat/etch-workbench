"""Regression for a GDS polygon that fills the whole GDS bounds (P0 SIGSEGV).

Native ``GDSGeometry.layerToLevelSet`` clips the polygon to the library frame,
so a polygon that spans the bounds has no inside/outside grid pair and reads
back as an **empty** level set.  AustriaLS ``Extrude`` on that field
dereferences a grid-less domain and the worker dies with SIGSEGV
(``returnCode: -11``), which is what run ``1bab74009b676ac2888356da`` hit.

The fixture ``tests/fixtures/gds_full_bounds_cell.gds`` is a static GDSII file
(generated once by ``tests/fixtures/make_gds_full_bounds_cell.py``; no test
imports gdstk or any GDS writer).  Its layer 7 polygon exactly fills the
structure bounds; layer 1 and layer 2 are ordinary inner shapes.

Covered here:
  * the geometry builder turns an empty planar interior into an explicit,
    layer-named error instead of reaching the crashing native Extrude;
  * a 2D and a 3D preview of the full-bounds fixture succeed with returnCode 0
    and recover the real full-cell body through a one-grid-cell padded native
    read (diagnostics: ``planarGridPadding``), not by inventing geometry.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from importlib.metadata import version
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import project as base_project
from worker.gds_mask import build_aligned_gds_layer, build_aligned_gds_mask

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "gds_full_bounds_cell.gds"
STRUCTURE = "FULLBOUNDS"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
DELTA = 0.2


class EmptyPlanarInteriorContract(unittest.TestCase):
    """The builder must fail loudly before the native Extrude can crash."""

    class _EmptyLevelSet:
        def getNumberOfPoints(self):
            return 0

    def test_empty_layer_interior_names_the_layer(self):
        with self.assertRaises(RuntimeError) as raised:
            # ps is None on purpose: the guard must run before any native call.
            build_aligned_gds_layer(None, None, self._EmptyLevelSet(), base_height=0.0,
                                    height=0.2, layer=7)
        message = str(raised.exception)
        self.assertIn("7", message)
        self.assertIn("gridDelta", message)

    def test_empty_mask_interior_names_the_layer(self):
        with self.assertRaises(RuntimeError) as raised:
            build_aligned_gds_mask(None, None, self._EmptyLevelSet(), base_height=0.0,
                                   height=0.2, retained_region="inside", layer=7)
        self.assertIn("7", str(raised.exception))


def gds_project(dimension: int, asset_id: str, layers: list[dict], *, name: str) -> dict:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = name
    value["domain"] = ({"gridDelta": DELTA, "xExtent": 6.0, "xBoundary": "reflective"}
                       if dimension == 2 else
                       {"gridDelta": DELTA, "xExtent": 6.0, "yExtent": 6.0,
                        "boundaryXY": "reflective"})
    value["geometry"] = {
        "kind": "gds", "assetId": asset_id, "fileName": FIXTURE.name,
        "structure": STRUCTURE, "unitScale": 1.0, "position": [0.0, 0.0, 0.0],
        "layers": [dict(row) for row in layers]}
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


class GdsFullBoundsPreview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()
        response = await asgi_request(
            "/api/gds-assets?file_name=" + FIXTURE.name, method="POST",
            payload=FIXTURE.read_bytes(),
            headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        metadata = json_body(response)
        self.asset_id = metadata["assetId"]
        self.assertEqual([row["name"] for row in metadata["structures"]], [STRUCTURE])

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def preview(self, project: dict) -> tuple[dict, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "preview", "project": project})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        for _ in range(2000):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("full-bounds GDS preview did not finish")
        self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
        # A SIGSEGV would show up as -11 here and the run could not be successful.
        self.assertEqual(info["termination"]["returnCode"], 0)
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["childRecord"]["returnCode"], 0)
        self.assertFalse(manifest["childRecord"]["timedOut"])
        snapshot = None
        for row in manifest["snapshots"]:
            path = snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])
            snapshot = storage.read_json(path)
            break
        self.assertIsNotNone(snapshot, "preview produced no snapshot")
        return snapshot, manifest

    async def test_3d_full_bounds_layer_preview_succeeds(self):
        project = gds_project(3, self.asset_id,
                              [{"layer": 7, "material": "Si", "baseHeight": -0.2,
                                "height": 0.4, "mask": False},
                               {"layer": 1, "material": "PolySi", "baseHeight": 0.2,
                                "height": 0.4, "mask": False}],
                              name="GDS 3D full-bounds layer preview")
        snapshot, manifest = await self.preview(project)
        self.assertEqual(snapshot["materialIdsByLevelSetIndex"], [10, 11])
        # The full-bounds polygon must become the real 4x4 um cell body, and the
        # inner layer must still be part of the cumulative top level set.
        si = material_bounds(snapshot, 10)
        poly = material_bounds(snapshot, 11)
        for axis, extent in (("x", 2.0), ("y", 2.0)):
            self.assertLessEqual(abs(si[axis][0] + extent), 2 * DELTA, (axis, si))
            self.assertLessEqual(abs(si[axis][1] - extent), 2 * DELTA, (axis, si))
        self.assertLessEqual(abs(si["z"][0] + 0.2), 2 * DELTA, si)
        self.assertLessEqual(abs(si["z"][1] - 0.2), 2 * DELTA, si)
        self.assertLessEqual(abs(poly["z"][0] - 0.2), 2 * DELTA, poly)
        self.assertLessEqual(abs(poly["z"][1] - 0.6), 2 * DELTA, poly)
        # Diagnostics prove the body came from a padded *native* read of the
        # real polygon, not from an unconditional full-domain fallback.
        with_snapshot = [row for row in manifest["snapshots"]]
        readback = with_snapshot[0]["geometryReadback"]
        constructions = {row["layer"]: row for row in readback["layerConstructions"]}
        self.assertEqual(readback["planarGridPadding"], DELTA)
        self.assertEqual(constructions[7]["planarGridPadding"], DELTA)
        self.assertGreater(constructions[7]["planarPointCount"], 0)
        self.assertGreater(constructions[7]["alignedLayerPointCount"], 0)
        # Layer 7 is the full-bounds polygon: its body comes from the padded
        # native 3D layerToLevelSet. Layer 1 used the installed 4.6.2 empty
        # native body for base 0.2 and needed the measured fallback; 4.7.0
        # returns a real body for the same layer, so the fallback is no longer
        # reached (the geometry assertions above already pin the result).
        self.assertEqual(constructions[7]["construction"], "native-layer-to-levelset-v1")
        self.assertIs(constructions[7]["nativeBodyUsed"], True)
        self.assertGreater(constructions[7]["nativeBodyPointCount"], 0)
        if version("viennaps") == "4.6.2":
            self.assertEqual(constructions[1]["construction"],
                             "native-extrude-capped-surface-regrid-union-v1")
            self.assertIs(constructions[1]["nativeBodyUsed"], False)
        else:
            self.assertEqual(constructions[1]["construction"], "native-layer-to-levelset-v1")
            self.assertIs(constructions[1]["nativeBodyUsed"], True)
            self.assertGreater(constructions[1]["nativeBodyPointCount"], 0)

    async def test_2d_full_bounds_layer_preview_succeeds(self):
        project = gds_project(2, self.asset_id,
                              [{"layer": 7, "material": "Si"},
                               {"layer": 1, "material": "PolySi"}],
                              name="GDS 2D full-bounds layer preview")
        snapshot, manifest = await self.preview(project)
        self.assertEqual(snapshot["materialIdsByLevelSetIndex"], [10, 11])
        si = material_bounds(snapshot, 10)
        self.assertTrue(si, snapshot["surfaceBounds"])
        self.assertLessEqual(abs(si["x"][0] + 2.0), 2 * DELTA, si)
        self.assertLessEqual(abs(si["x"][1] - 2.0), 2 * DELTA, si)
        # The 2D reader keeps the vertical boundary infinite, so the full-bounds
        # polygon already has an inside/outside pair there; the 2D fallback stays
        # a defensive guard and must not change this normal result.
        self.assertEqual(manifest["snapshots"][0]["geometryReadback"]["planarGridPadding"], 0.0)


if __name__ == "__main__":
    unittest.main()
