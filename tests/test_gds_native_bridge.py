"""Native GDS bridge acceptance: native bodies, padding, blur and booleans.

The contract tests stay pure Python. The native body comparison and every real
run go through the shipped supervisor/worker path (or a bounded native child),
so a SIGSEGV cannot be mistaken for a passing test.
"""

from __future__ import annotations

import asyncio
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.gds import MAX_GDS_BLUR_CELLS, blur_exposure_cell_count, parse_gds
from backend.hashing import requested_input, requested_input_hash
from backend.main import app
from backend.models import parse_project
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import directional_step, project as base_project

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "gds-mask-diagnosis" / "fixtures" / "multi-layer.gds"
PROBE = ROOT / "tests" / "fixtures" / "gds_native_bridge_probe.py"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
MATERIAL_SI = 10
BASELINE_HASH = "932cbc7b0c72736f64a676a0b20c5e0483943f3ea78962e437ad924b58810595"


def _gds_record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
    if data_type == 0x06 and len(payload) % 2:
        payload += b"\0"
    return struct.pack(">HBB", len(payload) + 4, kind, data_type) + payload


def _gds_real8(value: float) -> bytes:
    sign = 0x80 if value < 0 else 0
    normalized = abs(value)
    exponent = 0
    while normalized < 1 / 16:
        normalized *= 16
        exponent -= 1
    while normalized >= 1:
        normalized /= 16
        exponent += 1
    mantissa = int(normalized * (1 << 56))
    return bytes([sign | ((exponent + 64) & 0x7F)]) + mantissa.to_bytes(7, "big")


def _gds_rectangle(layer: int, x0: int, y0: int, x1: int, y1: int) -> bytes:
    return b"".join([
        _gds_record(0x08, 0x00),
        _gds_record(0x0D, 0x02, struct.pack(">h", layer)),
        _gds_record(0x0E, 0x02, struct.pack(">h", 0)),
        _gds_record(0x10, 0x03, struct.pack(">10i", x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)),
        _gds_record(0x11, 0x00),
    ])


def _gds_prefix(structure: str) -> bytes:
    timestamp = struct.pack(">12h", 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)
    return b"".join([
        _gds_record(0x00, 0x02, struct.pack(">h", 600)),
        _gds_record(0x01, 0x02, timestamp),
        _gds_record(0x02, 0x06, b"BLURBUDGET"),
        _gds_record(0x03, 0x05, _gds_real8(1e-3) + _gds_real8(1e-9)),
        _gds_record(0x05, 0x02, timestamp),
        _gds_record(0x06, 0x06, structure.encode("ascii")),
    ])


def gds_rectangle_bytes(width_nm: int, height_nm: int, *, layer: int = 1,
                        structure: str = "RECT") -> bytes:
    """One centered rectangle in nanometres (1 database unit = 1 nm)."""
    return b"".join([
        _gds_prefix(structure),
        _gds_rectangle(layer, -width_nm // 2, -height_nm // 2,
                       width_nm // 2, height_nm // 2),
        _gds_record(0x07, 0x00),
        _gds_record(0x04, 0x00),
    ])


def gds_reference_bytes() -> bytes:
    """TOP references LEAF, so Python cannot prove the native XY bounds."""
    timestamp = struct.pack(">12h", 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)
    leaf = b"".join([
        _gds_prefix("LEAF"), _gds_rectangle(1, -500, -500, 500, 500),
        _gds_record(0x07, 0x00),
    ])
    top = b"".join([
        _gds_record(0x05, 0x02, timestamp),
        _gds_record(0x06, 0x06, b"TOP"),
        _gds_record(0x0A, 0x00),
        _gds_record(0x12, 0x06, b"LEAF"),
        _gds_record(0x10, 0x03, struct.pack(">2i", 0, 0)),
        _gds_record(0x11, 0x00),
        _gds_record(0x07, 0x00),
    ])
    return leaf + top + _gds_record(0x04, 0x00)


def gds_project(dimension: int = 3, asset_id: str = "a" * 64, *,
                extent: float = 6.0, layers: list[dict] | None = None,
                blur: dict | None = None, delta: float = 0.1,
                steps: list[dict] | None = None,
                structure: str = "MULTI") -> dict:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = "GDS native bridge"
    value["domain"] = ({"gridDelta": delta, "xExtent": extent, "xBoundary": "reflective"}
                       if dimension == 2 else
                       {"gridDelta": delta, "xExtent": extent, "yExtent": extent,
                        "boundaryXY": "reflective"})
    value["geometry"] = {
        "kind": "gds", "assetId": asset_id, "fileName": FIXTURE.name,
        "structure": structure, "unitScale": 1.0, "position": [0.0, 0.0, 0.0],
        "layers": layers if layers is not None else
        ([{"layer": 1, "material": "Si"}]
         if dimension == 2 else
         [{"layer": 1, "material": "Si", "baseHeight": 0.0, "height": 0.6,
           "mask": False}]),
    }
    if blur is not None:
        value["geometry"]["blur"] = blur
    value["steps"] = steps or []
    value["outputSettings"] = {"levelSets": False, "levelSetMeshes": False,
                               "surfaceMeshVtk": False, "volumeMesh": False,
                               "nativeMetadata": False}
    value["solver"].update({"enginePreference": "cpu", "timeoutSeconds": 90,
                            "raysPerPoint": 32,
                            "random": {"mode": "fixed", "seed": 314159}})
    return value


def blur_block(**overrides) -> dict:
    value = {"sigmas": [5.0], "weights": [1.0], "threshold": 0.5,
             "delta": 0.1, "gridRefine": 4}
    value.update(overrides)
    return value


def material_bounds(snapshot: dict, material: int) -> dict:
    points = snapshot["points"]
    cells = snapshot.get("triangles") or snapshot.get("lines") or []
    ids = snapshot["cellData"]["materialIds"]
    vertices = {vertex for cell, mid in zip(cells, ids) if int(mid) == material
                for vertex in cell}
    if not vertices:
        return {}
    return {axis: [min(points[index][dimension] for index in vertices),
                   max(points[index][dimension] for index in vertices)]
            for axis, dimension in (("x", 0), ("y", 1), ("z", 2))}


def span(bounds: dict, axis: str) -> float:
    return bounds[axis][1] - bounds[axis][0]


class NativeBodyComparison(unittest.TestCase):
    """Native GDS bodies must match a directly built ViennaLS reference."""

    def test_native_2d_and_3d_bodies_match_direct_reference(self):
        temp = Path(tempfile.mkdtemp(prefix="gds-native-bridge-", dir=ROOT / "var"))
        self.addCleanup(lambda: __import__("shutil").rmtree(temp, ignore_errors=True))
        record = temp / "guard.json"
        log = temp / "probe.log"
        output = temp / "report.json"
        command = [
            str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
            "--cwd", str(ROOT), "--record", str(record), "--log-path", str(log),
            "--timeout-seconds", "60", "--max-rss-mib", "2048", "--",
            str(ROOT / ".venv/bin/python"), str(PROBE), str(FIXTURE), str(output),
        ]
        import subprocess
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=75)
        report = json.loads(output.read_text()) if output.exists() else {}
        self.assertEqual(run.returncode, 0, run.stderr[-800:])
        self.assertTrue(report.get("ok"), report)
        self.assertTrue(report["checks"]["2d"]["matches"], report["checks"]["2d"])
        self.assertTrue(report["checks"]["3d"]["matches"], report["checks"]["3d"])
        self.assertTrue(report["checks"]["materials"]["matches"],
                        report["checks"]["materials"])
        self.assertEqual(report["checks"]["3d"]["nativePoints"],
                         report["checks"]["3d"]["referencePoints"])


class BlurContract(unittest.TestCase):
    """Strict optional block; omission keeps the old request bytes."""

    def test_omitted_block_does_not_change_the_request_hash(self):
        project = gds_project(3)
        parsed = parse_project(project)
        self.assertNotIn("blur", requested_input(parsed)["geometry"])
        self.assertEqual(requested_input_hash(parsed), BASELINE_HASH)

    def test_blur_changes_the_hash_only_when_present(self):
        baseline = requested_input_hash(parse_project(gds_project(3)))
        blurred = gds_project(3, blur=blur_block())
        self.assertNotEqual(baseline, requested_input_hash(parse_project(blurred)))
        explicit = gds_project(3, blur=blur_block(enabled=True, gridRefine=4))
        self.assertEqual(requested_input_hash(parse_project(blurred)),
                         requested_input_hash(parse_project(explicit)))

    def test_unknown_length_range_and_finite_values_are_rejected(self):
        bad_blocks = [
            {**blur_block(), "unknown": 1},
            {"sigmas": [5.0], "weights": [1.0, 0.0]},
            {"sigmas": [], "weights": []},
            {"sigmas": [0.0], "weights": [1.0]},
            {"sigmas": [5.0], "weights": [-1.0]},
            {"sigmas": [5.0], "weights": [0.0]},
            blur_block(threshold=0.0),
            blur_block(threshold=1.0),
            blur_block(gridRefine=0),
            blur_block(gridRefine=9),
            blur_block(delta=0.2),
        ]
        for block in bad_blocks:
            with self.subTest(block=block):
                with self.assertRaises(Exception):
                    parse_project(gds_project(3, blur=block))

    def test_non_finite_values_are_rejected(self):
        for sigmas, weights in (([float("nan")], [1.0]), ([float("inf")], [1.0])):
            project = gds_project(3)
            project["geometry"]["blur"] = {"sigmas": sigmas, "weights": weights}
            with self.assertRaises(Exception):
                parse_project(project)


class GdsNativeBridgeRuns(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()
        response = await asgi_request(
            "/api/gds-assets?file_name=multi-layer.gds", method="POST",
            payload=FIXTURE.read_bytes(),
            headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        self.asset_id = json_body(response)["assetId"]

    async def upload_gds(self, file_name: str, data: bytes) -> str:
        response = await asgi_request(
            f"/api/gds-assets?file_name={file_name}", method="POST", payload=data,
            headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        return json_body(response)["assetId"]

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def run_project(self, project: dict, kind: str = "preview",
                          expected: str = "succeeded") -> tuple[dict, list[dict]]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": kind, "project": project})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        for _ in range(3000):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("GDS native bridge run did not finish")
        if expected == "failed":
            self.assertEqual(info["status"], "failed", info)
            return info, []
        self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        snapshots = []
        for row in manifest["snapshots"]:
            path = snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])
            snapshots.append(storage.read_json(path))
        info["_manifest"] = manifest
        return info, snapshots

    async def test_domain_smaller_than_gds_is_rejected_with_422(self):
        project = gds_project(3, self.asset_id, extent=2.0)
        with self.assertRaises(ProjectInputError) as caught:
            validate_project(project, kind="preview")
        self.assertIn("exceed the simulation domain", str(caught.exception))
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "preview", "project": project})
        self.assertEqual(response[0], 422, response[2])

        two_d = gds_project(2, self.asset_id, extent=2.0)
        with self.assertRaises(ProjectInputError):
            validate_project(two_d, kind="preview")

    async def test_blur_budget_uses_both_actual_gds_axes(self):
        # 4 x 4,000 um layout: the old xExtent-only estimate passed, while the
        # native padded layout allocates ~28.8 M fine cells.
        tall_asset = await self.upload_gds(
            "blur-tall.gds", gds_rectangle_bytes(4_000, 4_000_000))
        project = gds_project(2, tall_asset, extent=40.0, blur=blur_block(),
                              structure="RECT")

        with self.assertRaises(ProjectInputError) as caught:
            validate_project(project, kind="preview")
        self.assertIn("exposure grid", str(caught.exception))

        for path in ("/api/projects/validate",):
            response = await asgi_request(path, method="POST", payload=project)
            self.assertEqual(response[0], 422, response[2])
            detail = json.loads(response[2])["detail"]
            self.assertIn("geometry.blur", {row["path"] for row in detail})

        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "preview", "project": project})
        self.assertEqual(response[0], 422, response[2])

        project["steps"] = [directional_step(direction=[0.0, -1.0, 0.0])]
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("exposure grid", response[2].decode("utf-8", errors="replace"))

    async def test_blur_budget_accepts_wide_short_2d_layout(self):
        # 30 x 4 um layout in a 40 um domain: both actual axes fit the budget,
        # even though using xExtent for both axes would reject this request.
        wide_asset = await self.upload_gds(
            "blur-wide.gds", gds_rectangle_bytes(30_000, 4_000))
        project = gds_project(2, wide_asset, extent=40.0,
                              blur=blur_block(sigmas=[5.0] * 4,
                                              weights=[0.25] * 4, gridRefine=8),
                              structure="RECT")
        bounds = parse_gds(gds_rectangle_bytes(30_000, 4_000)).selected_layout_bounds_um("RECT")
        self.assertEqual(bounds, (-15.0, -2.0, 15.0, 2.0))
        cells = blur_exposure_cell_count(
            (bounds[0] - 0.2, bounds[1] - 0.2, bounds[2] + 0.2, bounds[3] + 0.2),
            grid_delta=0.1, exposure_delta=0.1, grid_refine=8, sigma_count=4)
        self.assertLess(cells, MAX_GDS_BLUR_CELLS)
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=project)
        self.assertEqual(response[0], 200, response[2])
        self.assertTrue(json_body(response)["valid"])

    async def test_blur_budget_fails_safe_when_layout_bounds_are_unknown(self):
        reference_asset = await self.upload_gds("blur-reference.gds",
                                                gds_reference_bytes())
        project = gds_project(2, reference_asset, extent=40.0, blur=blur_block(),
                              structure="TOP")
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=project)
        self.assertEqual(response[0], 422, response[2])
        detail = json.loads(response[2])["detail"]
        self.assertIn("geometry.blur", {row["path"] for row in detail})
        self.assertIn("SREF/AREF", response[2].decode("utf-8", errors="replace"))

    async def test_blur_overflow_and_underflow_are_422_not_500(self):
        overflow = gds_project(2, self.asset_id, extent=40.0,
                               blur=blur_block(sigmas=[5.0, 5.0],
                                               weights=[1e308, 1e308]))
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=overflow)
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("finite range", response[2].decode("utf-8", errors="replace"))

        underflow = gds_project(2, self.asset_id, extent=40.0,
                                blur=blur_block(delta=5e-324))
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=underflow)
        self.assertEqual(response[0], 422, response[2])
        body = response[2].decode("utf-8", errors="replace")
        # A subnormal exposure delta may be rejected as non-positive by the
        # schema or as an over-budget exposure grid; either is a normal 422.
        self.assertTrue("exposure grid" in body or "greater than 0" in body, body)

        tiny = gds_project(2, self.asset_id, extent=40.0,
                           blur=blur_block(delta=1e-300))
        response = await asgi_request("/api/projects/validate", method="POST",
                                      payload=tiny)
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("exposure grid", response[2].decode("utf-8", errors="replace"))

    async def test_2d_blur_threshold_effect_and_determinism(self):
        base = gds_project(2, self.asset_id)
        _, baseline = await self.run_project(base)
        baseline_bounds = material_bounds(baseline[0], MATERIAL_SI)
        self.assertAlmostEqual(span(baseline_bounds, "x"), 3.0, delta=0.3)

        _, wide = await self.run_project(gds_project(2, self.asset_id,
                                                     blur=blur_block(threshold=0.3)))
        _, narrow = await self.run_project(gds_project(2, self.asset_id,
                                                       blur=blur_block(threshold=0.7)))
        wide_bounds = material_bounds(wide[0], MATERIAL_SI)
        narrow_bounds = material_bounds(narrow[0], MATERIAL_SI)
        self.assertGreater(span(wide_bounds, "x"), span(baseline_bounds, "x") + 0.2)
        self.assertLess(span(narrow_bounds, "x"), span(baseline_bounds, "x") - 0.2)
        self.assertGreater(span(wide_bounds, "y"), span(narrow_bounds, "y"))

        first_project = gds_project(2, self.asset_id, blur=blur_block(threshold=0.5))
        first_info, first = await self.run_project(first_project)
        second_info, second = await self.run_project(copy.deepcopy(first_project))
        self.assertEqual(first_info["requestedInputHash"],
                         second_info["requestedInputHash"])
        self.assertEqual(material_bounds(first[0], MATERIAL_SI),
                         material_bounds(second[0], MATERIAL_SI))
        readback = first_info["_manifest"]["snapshots"][0]["geometryReadback"]
        blur = readback["blur"]
        self.assertEqual(blur["nativeMethod"], "GDSGeometry.addBlur")
        self.assertEqual(blur["threshold"], 0.5)
        self.assertEqual(blur["physicalSigmasUm"], [0.5])
        self.assertTrue(blur["regriddedToDomain"])
        self.assertIn("not a resist/OPC model", blur["semantics"])
        # The 2D product invariant survives blurred native reads.
        for snapshot in first:
            self.assertIn(MATERIAL_SI, {int(value)
                                        for value in snapshot["cellData"]["materialIds"]})

    async def test_3d_blur_uses_the_native_body_and_reports_values(self):
        project = gds_project(3, self.asset_id, blur=blur_block(sigmas=[5.0, 50.0],
                                                               weights=[0.8, 0.2]))
        info, snapshots = await self.run_project(project)
        readback = info["_manifest"]["snapshots"][0]["geometryReadback"]
        blur = readback["blur"]
        self.assertEqual(blur["sigmas"], [5.0, 50.0])
        self.assertEqual(blur["weights"], [0.8, 0.2])
        self.assertEqual(blur["gridRefinement"], 4)
        self.assertGreaterEqual(blur["nativeBodiesUsed"], 1)
        construction = readback["layerConstructions"][0]
        self.assertEqual(construction["construction"], "native-layer-to-levelset-v1")
        self.assertIs(construction["nativeBodyUsed"], True)
        self.assertEqual(construction["blur"]["threshold"], 0.5)
        si = material_bounds(snapshots[0], MATERIAL_SI)
        self.assertAlmostEqual(si["z"][0], 0.0, delta=0.2)
        self.assertAlmostEqual(si["z"][1], 0.6, delta=0.2)

    async def test_gds_boolean_union_and_intersection_keep_the_body(self):
        def project(operation: str, base: float) -> dict:
            step = {"id": "step_gds_operand", "name": "GDS operand", "enabled": True,
                    "model": "gdsBoolean", "booleanOperation": operation,
                    "applyToAll": True,
                    "gdsMask": {"layer": 2, "datatype": 0, "unitScale": 1.0,
                                "position": [0.0, 0.0, 0.0], "baseHeight": base,
                                "height": 0.6, "retainedRegion": "inside"}}
            return gds_project(3, self.asset_id, extent=4.0,
                               layers=[{"layer": 1, "material": "Si",
                                        "baseHeight": -0.6, "height": 1.2,
                                        "mask": False}],
                               steps=[step])
        for operation, base in (("union", 0.6), ("intersection", 0.0)):
            with self.subTest(operation=operation):
                info, snapshots = await self.run_project(project(operation, base),
                                                          kind="simulate")
                final = snapshots[-1]
                ids = {int(value) for value in final["cellData"]["materialIds"]}
                self.assertIn(MATERIAL_SI, ids)
                self.assertTrue(all(int(value) == MATERIAL_SI for value in
                                    final["materialIdsByLevelSetIndex"]))
                si = material_bounds(final, MATERIAL_SI)
                self.assertTrue(si)
                self.assertGreater(si["z"][1], si["z"][0])
                if operation == "intersection":
                    self.assertLessEqual(abs(si["x"][0] + 0.5), 0.3, si)
                    self.assertLessEqual(abs(si["x"][1] - 0.5), 0.3, si)
                else:
                    self.assertGreater(si["z"][1], 0.6, si)
                diagnostics = info["actualExecution"][-1]["actualParameters"]["maskDiagnostics"]
                self.assertIn(diagnostics["construction"],
                              {"native-layer-to-levelset-regrid-union-v1",
                               "native-extrude-capped-surface-regrid-union-v1"})


if __name__ == "__main__":
    unittest.main()
