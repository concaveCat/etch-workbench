"""Stage 5 acceptance: real ViennaPS geometry, GDS, extrusion, and slicing."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import struct
import subprocess
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from starlette.requests import Request

from backend import storage
from backend.main import app, continuation_package_export, continuation_package_import
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import project as base_project


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "stage5-evidence"
FIXTURE = EVIDENCE / "two-structure-two-layer.gds"
TERMINAL = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}


def project_v7(dimension: int, geometry: dict[str, Any], *, name: str) -> dict[str, Any]:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = name
    value["domain"]["gridDelta"] = 0.25
    value["domain"]["xExtent"] = 8.0 if dimension == 2 else 12.0
    if dimension == 3:
        value["domain"]["yExtent"] = 8.0
    value["geometry"] = copy.deepcopy(geometry)
    value["solver"].update({"raysPerPoint": 32, "enginePreference": "cpu",
                            "advection": {"spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
                                          "temporalScheme": "FORWARD_EULER", "adaptiveTimeStepping": False,
                                          "adaptiveTimeStepSubdivisions": 20, "calculateIntermediateVelocities": False,
                                          "checkDissipation": True, "dissipationAlpha": 1.0,
                                          "ignoreVoids": False, "velocityOutput": False},
                            "rayTracing": {"diskRadius": 0.0, "ignoreFluxBoundaries": False,
                                           "maxBoundaryHits": 1000, "maxReflections": 4294967295,
                                           "minNodeDistanceFactor": 0.05, "normalizationType": "SOURCE",
                                           "smoothingNeighbors": 1}})
    value["steps"] = []
    return value


def gds_geometry(asset_id: str, dimension: int, *, structure: str, layer: int,
                 position: list[float], unit_scale: float = 1.0) -> dict[str, Any]:
    row: dict[str, Any] = {"layer": layer, "material": "SiO2" if dimension == 2 else "W"}
    if dimension == 3:
            row.update({"baseHeight": 0.2, "height": 1.0, "mask": False})
    return {"kind": "gds", "assetId": asset_id, "fileName": "two-structure-two-layer.gds",
            "structure": structure, "unitScale": unit_scale, "position": position, "layers": [row]}


def signature(snapshot: dict[str, Any]) -> dict[str, Any]:
    topology_name = "triangles" if snapshot.get("dimension") == 3 or "triangles" in snapshot else "lines"
    return {"points": snapshot["points"], "topology": {topology_name: snapshot[topology_name]},
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(reference: dict[str, Any], key: str) -> dict[str, Any]:
    mesh = reference[key]
    topology_name = "triangles" if mesh["dimension"] == 3 else "lines"
    return {"points": mesh["points"], "topology": {topology_name: mesh["topology"][topology_name]},
            "materialIds": mesh["cellScalars"]["MaterialIds"],
            "materialIdsByLevelSetIndex": mesh["materialIdsByLevelSetIndex"]}


def hierarchical_gds_fixture() -> bytes:
    from backend.gds import parse_gds

    template = FIXTURE.read_bytes()
    library = parse_gds(template)
    prefix = template[:library.prefix_end]
    suffix = template[library.suffix_start:]

    def record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
        if data_type == 0x06 and len(payload) % 2:
            payload += b"\0"
        return struct.pack(">HBB", len(payload) + 4, kind, data_type) + payload

    timestamp = struct.pack(">12h", 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)

    def polygon(name: str, layer: int, bounds: tuple[int, int, int, int]) -> bytes:
        x0, y0, x1, y1 = bounds
        return (record(0x05, 0x02, timestamp) + record(0x06, 0x06, name.encode("ascii"))
                + record(0x08, 0x00) + record(0x0D, 0x02, struct.pack(">h", layer))
                + record(0x0E, 0x02, struct.pack(">h", 0))
                + record(0x10, 0x03, struct.pack(">10i", x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))
                + record(0x11, 0x00) + record(0x07, 0x00))

    leaf = polygon("LEAF_A", 3, (0, 0, 2000, 1000))
    unrelated = polygon("UNRELATED", 3, (0, 0, 1000, 1000))
    top = (record(0x05, 0x02, timestamp) + record(0x06, 0x06, b"TOP")
           + record(0x0A, 0x00) + record(0x12, 0x06, b"LEAF_A")
           + record(0x10, 0x03, struct.pack(">2i", 3000, 0))
           + record(0x11, 0x00) + record(0x07, 0x00))
    return prefix + leaf + unrelated + top + suffix


class Stage5Acceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.life.__aexit__(None, None, None)

    async def wait_run(self, run_id: str, timeout: float = 90.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/runs/{run_id}")
            self.assertEqual(response[0], 200, response[2].decode("utf-8", errors="replace"))
            value = json_body(response)
            if value["status"] in TERMINAL:
                self.assertEqual(value["status"], "succeeded", value.get("failureReason"))
                manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
                self.assertEqual(manifest["childRecord"]["remainingOwnedGroupMembers"], [])
                self.assertFalse(manifest["childRecord"]["timedOut"])
                self.assertFalse(manifest["childRecord"]["rssLimited"])
                return value
            await asyncio.sleep(0.05)
        self.fail(f"bounded ViennaPS run {run_id} did not finish in {timeout} seconds")

    async def direct_reference(self, project_value: dict[str, Any], label: str, *,
                               gds_path: Path | None = None,
                               slice_request: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
        folder = EVIDENCE / f"direct-{label}"
        folder.mkdir(parents=True, exist_ok=True)
        request_path = EVIDENCE / f"{label}-direct-input.json"
        output_path = folder / "reference.json"
        record_path = EVIDENCE / f"{label}-direct.guard.json"
        log_path = EVIDENCE / f"{label}-direct.log"
        request = {"project": project_value, "gdsPath": str(gds_path) if gds_path else None,
                   "sliceRequest": slice_request}
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(record_path), "--log-path", str(log_path),
                   "--timeout-seconds", "60", "--max-rss-mib", "2048", "--max-log-bytes", "4194304", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage5_geometry_reference.py"),
                   str(request_path), str(output_path)]
        child = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await child.wait(), 0, log_path.read_text(encoding="utf-8", errors="replace"))
        guard = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertFalse(guard["timedOut"])
        self.assertFalse(guard["rssLimited"])
        self.assertEqual(guard["remainingOwnedGroupMembers"], [])
        return json.loads(output_path.read_text(encoding="utf-8")), output_path.parent

    async def upload_fixture(self) -> tuple[str, dict[str, Any]]:
        response = await asgi_request("/api/gds-assets?file_name=two-structure-two-layer.gds", method="POST",
                                      payload=FIXTURE.read_bytes(),
                                      headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(response[0], 201, response[2].decode("utf-8", errors="replace"))
        metadata = json_body(response)
        self.assertEqual(metadata["structures"], [
            {"name": "CELL_A", "layers": [1], "layerDatatypes": [[1, 0]],
             "maskLayerDatatypes": [[1, 0]], "references": []},
            {"name": "CELL_B", "layers": [2], "layerDatatypes": [[2, 0]],
             "maskLayerDatatypes": [[2, 0]], "references": []},
        ])
        self.assertEqual(metadata["assetId"], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        return metadata["assetId"], metadata

    async def save_and_reopen(self, project_value: dict[str, Any]) -> dict[str, Any]:
        created = await asgi_request("/api/projects", method="POST", payload=project_value)
        self.assertEqual(created[0], 201, created[2].decode("utf-8", errors="replace"))
        project_id = json_body(created)["id"]
        opened = await asgi_request(f"/api/projects/{project_id}")
        self.assertEqual(opened[0], 200)
        reopened = json_body(opened)["project"]
        self.assertEqual(reopened["schemaVersion"], 7)
        self.assertEqual(reopened["geometry"], project_value["geometry"])
        return reopened

    async def run_preview(self, project_value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "preview", "project": project_value})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        return run_id, await self.wait_run(run_id)

    async def consume_package(self, response: Any) -> bytes:
        try:
            return b"".join([chunk async for chunk in response.body_iterator])
        finally:
            if response.background is not None:
                response.background.func(*response.background.args, **response.background.kwargs)

    async def import_package(self, package: bytes) -> dict[str, Any]:
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": package, "more_body": False}

        request = Request({"type": "http", "method": "POST", "scheme": "http",
                           "path": "/api/continuation-package/import",
                           "headers": [(b"content-type", b"application/zip"),
                                       (b"content-length", str(len(package)).encode("ascii"))]}, receive)
        try:
            return await continuation_package_import(request)
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                self.fail(f"portable GDS project import failed ({exc.status_code}): {exc.detail}")
            raise

    async def test_gds_upload_selection_native_geometry_portable_package_and_replay(self) -> None:
        asset_id, metadata = await self.upload_fixture()
        cases = [
            (2, "CELL_A", 1, [0.25, 0.1, 0.0], 1.5,
             {"layer": 1, "material": "SiO2"}),
            (3, "CELL_B", 2, [-0.25, 0.25, 0.1], 1.0,
             {"layer": 2, "material": "W", "baseHeight": 0.2, "height": 0.75, "mask": False}),
        ]
        accepted: list[dict[str, Any]] = []
        imported_package: bytes | None = None
        imported_replay: dict[str, Any] | None = None
        imported_expected: dict[str, Any] | None = None
        imported_dimension = 0
        for dimension, structure, layer, position, unit_scale, mapping in cases:
            with self.subTest(dimension=dimension, structure=structure):
                geometry = gds_geometry(asset_id, dimension, structure=structure, layer=layer,
                                        position=position, unit_scale=unit_scale)
                project_value = project_v7(dimension, geometry, name=f"Stage 5 {dimension}D {structure}")
                check = await asgi_request("/api/projects/validate", method="POST", payload=project_value)
                self.assertEqual(check[0], 200, check[2].decode("utf-8", errors="replace"))
                reopened = await self.save_and_reopen(project_value)

                native_inspect = await asgi_request(
                    f"/api/gds-assets/{asset_id}/inspect", method="POST",
                    payload={"structure": structure, "dimension": dimension, "gridDelta": 0.25,
                             "boundary": "reflective", "unitScale": unit_scale,
                             "position": position[:2], "selectedLayers": [layer], "probeHeight": 1.0})
                self.assertEqual(native_inspect[0], 200, native_inspect[2].decode("utf-8", errors="replace"))
                inspect_result = json_body(native_inspect)
                self.assertEqual(inspect_result["inspection"]["selectedNativeLayers"], [layer])
                self.assertTrue(inspect_result["inspection"]["supportLayers"])
                self.assertEqual(inspect_result["boundedChild"]["returnCode"], 0)

                direct, direct_dir = await self.direct_reference(
                    reopened, f"gds-{dimension}d-{structure}", gds_path=storage.gds_asset_path(asset_id))
                run_id, run_info = await self.run_preview(reopened)
                initial_response = await asgi_request(f"/api/runs/{run_id}/snapshots/initial")
                self.assertEqual(initial_response[0], 200, initial_response[2])
                initial = json_body(initial_response)
                self.assertEqual(signature(initial), direct_signature(direct, "initial"))
                self.assertEqual(initial["materialIdsByLevelSetIndex"], direct["materialIdsByLevelSetIndex"])
                self.assertEqual(initial["geometryReadback"]["selectedNativeLayers"], [layer])
                self.assertEqual(initial["geometryReadback"]["nativeBounds"], direct["geometryReadback"]["nativeBounds"])
                self.assertEqual(run_info["snapshots"][0]["geometryReadback"]["selectedNativeLayers"], [layer])

                download = await asgi_request(f"/api/runs/{run_id}/download")
                self.assertEqual(download[0], 200)
                with zipfile.ZipFile(BytesIO(download[2])) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertIn("project.json", archive.namelist())
                    self.assertIn(f"assets/{asset_id}.gds", archive.namelist())
                    self.assertIn("gds-selected.gds", archive.namelist())
                    transform = json.loads(archive.read("gds-transform.json"))
                    self.assertEqual(transform["selectedStructure"], structure)
                    self.assertEqual(transform["selectedNativeLayers"], [layer])
                    self.assertEqual(hashlib.sha256(archive.read("gds-selected.gds")).hexdigest(),
                                     transform["filteredFileSha256"])
                    replay = json.loads(archive.read("replay-project.json"))
                history = json_body(await asgi_request("/api/runs"))["runs"]
                self.assertTrue(any(row["runId"] == run_id for row in history))
                replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(replay_response[0], 202, replay_response[2].decode("utf-8", errors="replace"))
                replay_id = json_body(replay_response)["runId"]
                await self.wait_run(replay_id)
                replay_snapshot = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/initial"))
                self.assertEqual(signature(replay_snapshot), direct_signature(direct, "initial"))
                self.assertEqual(replay_snapshot["geometryReadback"]["filteredFileSha256"],
                                 initial["geometryReadback"]["filteredFileSha256"])
                accepted.append({"dimension": dimension, "structure": structure, "layer": layer,
                                 "runId": run_id, "replayRunId": replay_id,
                                 "meshPointCount": len(initial["points"]),
                                 "materialIdsByLevelSetIndex": initial["materialIdsByLevelSetIndex"],
                                 "nativeBounds": initial["geometryReadback"]["nativeBounds"],
                                 "directNativeMeshExact": True,
                                 "saveReopenReplayAndRunZip": True,
                                 "directOutput": str(direct_dir / "reference.json")})
                if dimension == 3:
                    slice_request = {"axis": 1, "position": 1.25}
                    sliced_direct, slice_dir = await self.direct_reference(
                        reopened, "gds-3d-true-slice", gds_path=storage.gds_asset_path(asset_id),
                        slice_request=slice_request)
                    slice_start = await asgi_request("/api/runs/slice", method="POST",
                                                     payload={"project": reopened, "sliceRequest": slice_request})
                    self.assertEqual(slice_start[0], 202, slice_start[2].decode("utf-8", errors="replace"))
                    slice_run_id = json_body(slice_start)["runId"]
                    await self.wait_run(slice_run_id)
                    sliced_snapshot = json_body(await asgi_request(f"/api/runs/{slice_run_id}/snapshots/true-slice"))
                    self.assertEqual(signature(sliced_snapshot), direct_signature(sliced_direct, "slice"))
                    self.assertEqual(sliced_snapshot["sliceReadback"]["materialIdsByLevelSetIndex"],
                                     sliced_direct["sliceReadback"]["materialIdsByLevelSetIndex"])
                    accepted[-1].update({"sliceRunId": slice_run_id,
                                         "sliceMeshPointCount": len(sliced_snapshot["points"]),
                                         "directNativeSliceExact": True,
                                         "directSliceOutput": str(slice_dir / "reference.json")})
                if dimension == 2:
                    replay_doc = json.loads((storage.run_path(run_id) / "replay-project.json").read_text())
                    package_response = await continuation_package_export({"project": reopened, "replay": replay_doc})
                    package = await self.consume_package(package_response)
                    with zipfile.ZipFile(BytesIO(package)) as archive:
                        self.assertEqual(set(archive.namelist()), {"project.json", "replay-project.json", f"assets/{asset_id}.gds"})
                        self.assertEqual(archive.read(f"assets/{asset_id}.gds"), FIXTURE.read_bytes())
                        self.assertNotIn(str(ROOT), archive.read("project.json").decode("utf-8"))
                    storage.gds_asset_path(asset_id).unlink()
                    (storage.GDS_ASSETS / f"{asset_id}.json").unlink(missing_ok=True)
                    imported = await self.import_package(package)
                    self.assertEqual(imported["project"]["geometry"]["assetId"], asset_id)
                    self.assertEqual(imported["replay"]["normalizedProject"]["geometry"]["assetId"], asset_id)
                    imported_package, imported_replay = package, imported["replay"]
                    imported_expected, imported_dimension = direct, dimension

        self.assertIsNotNone(imported_package)
        self.assertIsNotNone(imported_replay)
        replay_start = await asgi_request("/api/runs/replay", method="POST", payload=imported_replay)
        self.assertEqual(replay_start[0], 202, replay_start[2].decode("utf-8", errors="replace"))
        imported_run_id = json_body(replay_start)["runId"]
        await self.wait_run(imported_run_id)
        imported_snapshot = json_body(await asgi_request(f"/api/runs/{imported_run_id}/snapshots/initial"))
        self.assertEqual(signature(imported_snapshot), direct_signature(imported_expected, "initial"))
        (EVIDENCE / "gds-acceptance.json").write_text(json.dumps({
            "assetId": asset_id, "sourceSha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "sourceMetadata": metadata, "cases": accepted, "portableImportReplayRunId": imported_run_id,
            "portableImportReplayExact": True, "passed": True,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def test_gds_cell_dependency_and_same_layer_isolation(self) -> None:
        source = EVIDENCE / "hierarchical-reference.gds"
        source.write_bytes(hierarchical_gds_fixture())
        upload = await asgi_request("/api/gds-assets?file_name=hierarchical-reference.gds", method="POST",
                                    payload=source.read_bytes(),
                                    headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(upload[0], 201, upload[2].decode("utf-8", errors="replace"))
        metadata = json_body(upload)
        self.assertEqual(metadata["structures"], [
            {"name": "LEAF_A", "layers": [3], "layerDatatypes": [[3, 0]],
             "maskLayerDatatypes": [[3, 0]], "references": []},
            {"name": "UNRELATED", "layers": [3], "layerDatatypes": [[3, 0]],
             "maskLayerDatatypes": [[3, 0]], "references": []},
            {"name": "TOP", "layers": [], "layerDatatypes": [],
             "maskLayerDatatypes": [], "references": ["LEAF_A"]},
        ])
        asset_id = metadata["assetId"]
        geometry = {"kind": "gds", "assetId": asset_id, "fileName": source.name,
                    "structure": "TOP", "unitScale": 1.0, "position": [0.0, 0.0, 0.0],
                    "layers": [{"layer": 3, "material": "SiO2"}]}
        project_value = project_v7(2, geometry, name="Stage 5 hierarchical GDS dependency")
        project_value["domain"]["xExtent"] = 12.0
        validation = await asgi_request("/api/projects/validate", method="POST", payload=project_value)
        self.assertEqual(validation[0], 200, validation[2].decode("utf-8", errors="replace"))
        direct, direct_dir = await self.direct_reference(
            project_value, "gds-hierarchical-dependency", gds_path=storage.gds_asset_path(asset_id))
        run_id, run_info = await self.run_preview(project_value)
        snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/initial"))
        self.assertEqual(signature(snapshot), direct_signature(direct, "initial"))
        self.assertEqual(snapshot["geometryReadback"]["includedStructures"], ["LEAF_A", "TOP"])
        self.assertEqual(snapshot["geometryReadback"]["supportStructures"], ["UNRELATED"])
        self.assertEqual(snapshot["geometryReadback"]["selectedNativeLayers"], [3])
        self.assertTrue(all(point[0] >= 3.0 - 1e-10 for point in snapshot["points"]),
                        "the unrelated same-layer cell at x=0..1 must not leak into TOP")
        self.assertEqual(run_info["status"], "succeeded")
        (EVIDENCE / "hierarchical-gds-acceptance.json").write_text(json.dumps({
            "assetId": asset_id, "runId": run_id,
            "selectedStructure": "TOP", "dependencyClosure": snapshot["geometryReadback"]["includedStructures"],
            "unrelatedSameLayerStructure": "UNRELATED", "nativeMeshPointCount": len(snapshot["points"]),
            "unrelatedSameLayerGeometryExcluded": True, "directViennaPSMeshExact": True,
            "directOutput": str(direct_dir / "reference.json"), "passed": True,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def test_fin_native_2d_3d_extrusion_and_true_slice(self) -> None:
        cases = [
            project_v7(2, {"kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0.0,
                           "maskHeight": 0.0, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                           "maskMaterial": "Mask"}, name="Stage 5 native 2D Fin"),
            project_v7(3, {"kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0.0,
                           "maskHeight": 0.0, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                           "maskMaterial": "Mask", "extrusion": {"extent": [-1.0, 1.0], "axis": 1}},
                       name="Stage 5 extruded 3D Fin"),
        ]
        accepted = []
        for project_value in cases:
            dimension = project_value["dimension"]
            direct, direct_dir = await self.direct_reference(project_value, f"fin-{dimension}d")
            run_id, run_info = await self.run_preview(project_value)
            initial = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/initial"))
            self.assertEqual(signature(initial), direct_signature(direct, "initial"))
            self.assertIn("MakeFin", initial["geometryReadback"]["nativeGeometry"])
            accepted.append({"dimension": dimension, "runId": run_id, "nativeGeometry": initial["geometryReadback"]["nativeGeometry"],
                             "meshPointCount": len(initial["points"]), "directNativeMeshExact": True,
                             "directOutput": str(direct_dir / "reference.json")})
            if dimension == 3:
                request = {"axis": 1, "position": 0.0}
                sliced_direct, slice_dir = await self.direct_reference(project_value, "fin-3d-true-slice", slice_request=request)
                start = await asgi_request("/api/runs/slice", method="POST",
                                           payload={"project": project_value, "sliceRequest": request})
                self.assertEqual(start[0], 202, start[2].decode("utf-8", errors="replace"))
                slice_run_id = json_body(start)["runId"]
                slice_info = await self.wait_run(slice_run_id)
                slice_snapshot = json_body(await asgi_request(f"/api/runs/{slice_run_id}/snapshots/true-slice"))
                self.assertEqual(signature(slice_snapshot), direct_signature(sliced_direct, "slice"))
                readback = slice_snapshot["sliceReadback"]
                self.assertEqual(readback["nativeOperation"], "viennaps.Slice")
                self.assertEqual(readback["axis"], 1)
                self.assertEqual(readback["sourceDimension"], 3)
                self.assertEqual(readback["outputDimension"], 2)
                self.assertFalse(readback["viewerClipping"])
                self.assertEqual(readback["materialIdsByLevelSetIndex"], sliced_direct["sliceReadback"]["materialIdsByLevelSetIndex"])
                self.assertEqual(slice_info["sliceRequest"], request)
                zip_response = await asgi_request(f"/api/runs/{slice_run_id}/download")
                with zipfile.ZipFile(BytesIO(zip_response[2])) as archive:
                    replay = json.loads(archive.read("replay-project.json"))
                    self.assertEqual(replay["analysisMode"], "slice")
                    self.assertEqual(replay["sliceRequest"], request)
                replay_start = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(replay_start[0], 202, replay_start[2].decode("utf-8", errors="replace"))
                slice_replay_id = json_body(replay_start)["runId"]
                await self.wait_run(slice_replay_id)
                replay_snapshot = json_body(await asgi_request(f"/api/runs/{slice_replay_id}/snapshots/true-slice"))
                self.assertEqual(signature(replay_snapshot), direct_signature(sliced_direct, "slice"))
                accepted[-1].update({"sliceRunId": slice_run_id, "sliceReplayRunId": slice_replay_id,
                                     "sliceMeshPointCount": len(slice_snapshot["points"]),
                                     "directNativeSliceExact": True, "viewerClipping": False,
                                     "directSliceOutput": str(slice_dir / "reference.json")})
        (EVIDENCE / "fin-slice-acceptance.json").write_text(json.dumps({"cases": accepted, "passed": True},
                                                                         ensure_ascii=False, indent=2) + "\n",
                                                             encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
