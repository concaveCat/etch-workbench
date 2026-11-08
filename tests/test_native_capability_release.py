"""Native capability release: GPU_TRIANGLE, any-axis Slice, half geometries.

These tests compare the real Workbench worker against independent direct
ViennaPS calls. GPU cases run on the host GPU when the bounded CUDA inventory
is available in this sandbox; otherwise they skip with the device reason while
the validation/routing contracts still run.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EVIDENCE = ROOT / "var" / "evidence"
GPU_TRIANGLE_DIR = ROOT / "docs" / "stage9-evidence" / "gpu-variants"
DEVICE = {"uuid": "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
          "name": "NVIDIA GeForce RTX 3060 Laptop GPU", "pciBusId": "0000:01:00.0",
          "logicalDevice": 0}
GPU_INVENTORY = {"status": "available", "devices": [DEVICE],
                 "preferredGpuDeviceId": DEVICE["uuid"], "reason": None,
                 "probe": {"source": "native-capability-release-test"}}

from backend import snapshots as snapshot_store, storage
from backend.hashing import requested_input_hash
from backend.main import app
from backend.models import SliceRequestV7, parse_project
from backend.validation import ProjectInputError, gpu_project_signature, select_engine, validate_project
from tests.test_api_integration import asgi_request, json_body
from tests.test_stage5 import project_v7


def signature(snapshot: dict) -> dict:
    topology = "triangles" if snapshot["dimension"] == 3 else "lines"
    return {"points": snapshot["points"], f"topology/{topology}": snapshot[topology],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(reference: dict, key: str) -> dict:
    mesh = reference[key]
    dimension = mesh.get("dimension", reference.get("dimension"))
    topology = "triangles" if dimension == 3 else "lines"
    cells = mesh["topology"][topology] if "topology" in mesh else mesh["cells"]
    material_ids = (mesh["cellScalars"]["MaterialIds"] if "cellScalars" in mesh
                    else mesh["materialIds"])
    return {"points": mesh["points"], f"topology/{topology}": cells,
            "materialIds": material_ids,
            "materialIdsByLevelSetIndex": mesh["materialIdsByLevelSetIndex"]}


def bounds(snapshot: dict) -> dict:
    points = snapshot["points"]
    return {axis: [min(point[index] for point in points), max(point[index] for point in points)]
            for index, axis in enumerate(("x", "h"))}


def comparison_tolerance(reference: dict, installed: str) -> float | None:
    """Exact match for same-runtime references, float32 ULP tolerance across runtimes.

    The archived GPU_TRIANGLE references were measured on ViennaPS 4.6.2. The
    4.7.0 kernel returns the same topology and materials but coordinates that
    differ by ~3e-9 um (single-precision rounding), so a cross-runtime replay
    compares numerically instead of demanding bit identity.
    """
    measured = (reference.get("versions") or {}).get("viennaps")
    return None if measured == installed else 1e-6


def assert_signature_matches(testcase: unittest.TestCase, actual: dict, expected: dict,
                            tolerance: float | None) -> None:
    for key in ("materialIds", "materialIdsByLevelSetIndex"):
        testcase.assertEqual(actual[key], expected[key], key)
    topology = [key for key in actual if key.startswith("topology/")]
    for key in topology:
        testcase.assertEqual(actual[key], expected[key], key)
    if tolerance is None:
        testcase.assertEqual(actual["points"], expected["points"])
        return
    testcase.assertEqual(len(actual["points"]), len(expected["points"]))
    worst = max((abs(float(a[i]) - float(b[i])) for a, b in zip(actual["points"], expected["points"])
                 for i in range(len(a))), default=0.0)
    testcase.assertLessEqual(worst, tolerance,
                             f"largest coordinate deviation from the archived reference: {worst:g}")


class NativeCapabilityRelease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.life.__aexit__(None, None, None)

    async def wait_run(self, run_id: str, timeout: float = 300.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/runs/{run_id}")
            self.assertEqual(response[0], 200, response[2].decode("utf-8", errors="replace"))
            value = json_body(response)
            if value["status"] in ("succeeded", "failed", "cancelled", "timed_out", "interrupted"):
                return value
            await asyncio.sleep(0.05)
        self.fail(f"run {run_id} did not finish within {timeout} seconds")

    async def snapshot(self, run_id: str, step_id: str) -> dict:
        snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(storage.run_path(run_id) / f"{step_id}.json")

    async def run_preview(self, project: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "preview", "project": project})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        result = await self.wait_run(run_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        return run_id, result

    async def direct_reference(self, project: dict, label: str, *,
                               slice_request: dict | None = None) -> dict:
        folder = EVIDENCE / f"capability-direct-{label}"
        folder.mkdir(parents=True, exist_ok=True)
        request_path = folder / "input.json"
        output_path = folder / "reference.json"
        record_path = folder / "guard.json"
        log_path = folder / "direct.log"
        request_path.write_text(json.dumps({"project": project, "sliceRequest": slice_request},
                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(record_path), "--log-path", str(log_path),
                   "--timeout-seconds", "120", "--max-rss-mib", "2048",
                   "--max-log-bytes", "4194304", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage5_geometry_reference.py"),
                   str(request_path), str(output_path)]
        child = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await child.wait(), 0, log_path.read_text(encoding="utf-8", errors="replace"))
        guard = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(guard["returnCode"], 0)
        self.assertFalse(guard["timedOut"])
        self.assertFalse(guard["rssLimited"])
        self.assertEqual(guard["remainingOwnedGroupMembers"], [])
        return json.loads(output_path.read_text(encoding="utf-8"))

    # ---------------------------------------------------------------- GPU_TRIANGLE
    def gpu_triangle_project(self, dimension: int) -> dict:
        path = GPU_TRIANGLE_DIR / f"gpu-triangle-{dimension}d" / f"sf6o2-{dimension}d-gpu_triangle.final.input.json"
        if not path.is_file():
            self.skipTest(f"GPU_TRIANGLE {dimension}D evidence is not archived in this worktree")
        project = json.loads(path.read_text(encoding="utf-8"))
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_TRIANGLE"
        project["solver"]["gpuDeviceId"] = DEVICE["uuid"]
        project["solver"]["timeoutSeconds"] = 240
        return project

    def test_gpu_triangle_matrix_rows_cover_only_the_measured_pair(self):
        matrix = json.loads((ROOT / "docs/stage9-evidence/gpu-capability-matrix.json").read_text())
        rows = [row for row in matrix["rows"] if row["engine"] == "GPU_TRIANGLE"]
        self.assertEqual({(row["dimension"], row["model"], row["operation"], row["boundaryXY"])
                          for row in rows},
                         {(2, "specialized:sf6o2", "etch", "reflective"),
                          (3, "specialized:sf6o2", "etch", "reflective")})
        self.assertEqual({tuple(row["geometryKinds"]) for row in rows}, {("stack",)})
        self.assertEqual({tuple(row.get("patternKinds", [])) for row in rows}, {("none",)})
        self.assertEqual({tuple(row["materials"]) for row in rows}, {("Si", "Si3N4", "SiO2")})
        for row in rows:
            for field in ("productWorker", "productGuard", "directReference", "directGuard"):
                reference = row["evidence"][field]
                self.assertTrue((ROOT / reference).is_file(), reference)

    def test_gpu_triangle_validation_rejects_unproven_combinations_without_fallback(self):
        project = self.gpu_triangle_project(2)
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        self.assertEqual(normalized["solver"]["gpuEngine"], "GPU_TRIANGLE")
        self.assertEqual(select_engine(normalized, normalized["steps"][0]), "GPU_TRIANGLE")
        triangle_hash = requested_input_hash(parsed)
        disk_project = copy.deepcopy(project)
        disk_project["solver"]["gpuEngine"] = "GPU_DISK"
        disk_hash = requested_input_hash(parse_project(disk_project))
        self.assertNotEqual(triangle_hash, disk_hash,
                            "requested hash must preserve the selected GPU engine identity")

        unsupported = copy.deepcopy(project)
        unsupported["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
        with self.assertRaises(ProjectInputError) as context:
            validate_project(unsupported, kind="simulate")
        messages = " ".join(issue.message for issue in context.exception.issues)
        self.assertIn("GPU_TRIANGLE has no direct-reference acceptance row", messages)
        self.assertIn("instead of falling back", messages)

        wrong_preference = copy.deepcopy(project)
        wrong_preference["solver"]["enginePreference"] = "auto"
        with self.assertRaises(ProjectInputError) as context:
            validate_project(wrong_preference, kind="simulate")
        self.assertTrue(any("gpuEngine requires" in issue.message for issue in context.exception.issues))

        normalized_unsupported = copy.deepcopy(normalized)
        normalized_unsupported["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
        self.assertEqual(select_engine(normalized_unsupported, normalized_unsupported["steps"][0]), "unavailable")

    async def test_gpu_triangle_real_2d_and_3d_product_direct_and_replay(self):
        for dimension in (2, 3):
            project = self.gpu_triangle_project(dimension)
            direct_path = (GPU_TRIANGLE_DIR / f"gpu-triangle-{dimension}d" /
                           f"sf6o2-{dimension}d-gpu_triangle.final.direct-reference.json")
            if not direct_path.is_file():
                self.skipTest(f"GPU_TRIANGLE {dimension}D direct reference is not archived")
            direct = json.loads(direct_path.read_text(encoding="utf-8"))
            with patch("backend.supervisor.cuda_devices", return_value=GPU_INVENTORY):
                response = await asgi_request("/api/runs", method="POST",
                                              payload={"kind": "simulate", "project": project})
                self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
                run_id = json_body(response)["runId"]
                result = await self.wait_run(run_id)
            if result["status"] != "succeeded":
                reason = str(result.get("failureReason") or "")
                if any(token in reason for token in ("CUDA", "GPU", "gpu", "device", "cuda")):
                    self.skipTest(f"GPU unavailable in this sandbox: {reason.strip()}")
                self.fail(reason)
            manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
            self.assertEqual(manifest["plannedExecution"][0]["engine"], "GPU_TRIANGLE")
            self.assertEqual(manifest["plannedExecution"][0]["gpuDeviceId"], DEVICE["uuid"])
            self.assertEqual(manifest["actualExecution"][-1]["actualEngine"], "GPU_TRIANGLE")
            readback = manifest["actualExecution"][-1]["actualParameters"]["cudaDeviceReadback"]
            self.assertEqual(readback["uuid"], DEVICE["uuid"])
            self.assertEqual(readback["readbackStatus"], "verified")
            product = await self.snapshot(run_id, "stage8_sf6o2")
            tolerance = comparison_tolerance(direct, version("viennaps"))
            assert_signature_matches(self, signature(product), direct_signature(direct, "final"),
                                     tolerance)

            replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
            self.assertEqual(replay["executionRequirements"]["steps"][0]["engine"], "GPU_TRIANGLE")
            self.assertEqual(replay["normalizedProject"]["solver"]["gpuEngine"], "GPU_TRIANGLE")
            with patch("backend.supervisor.cuda_devices", return_value=GPU_INVENTORY):
                replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(replay_response[0], 202,
                                 replay_response[2].decode("utf-8", errors="replace"))
                replay_id = json_body(replay_response)["runId"]
                replay_result = await self.wait_run(replay_id)
            self.assertEqual(replay_result["status"], "succeeded", replay_result.get("failureReason"))
            repeated = await self.snapshot(replay_id, "stage8_sf6o2")
            self.assertEqual(signature(repeated), signature(product))
            (EVIDENCE / "capability-gpu-triangle.json").write_text(json.dumps({
                "case": f"{dimension}D", "runId": run_id, "replayRunId": replay_id,
                "engine": "GPU_TRIANGLE", "gpuDeviceId": DEVICE["uuid"],
                "productDirectExact": tolerance is None,
                "productDirectMatch": "exact" if tolerance is None else "1e-6-um",
                "referenceVersions": direct.get("versions"),
                "installedVersions": {"viennaps": version("viennaps"),
                                      "viennals": version("viennals")},
                "replayExact": True,
                "directReference": str(direct_path.relative_to(ROOT)),
            }, indent=2) + "\n", encoding="utf-8")

    def run_direct_worker(self, project: dict, *, step_engines: dict | None = None,
                          device: bool = True, timeout: int = 240) -> tuple[int, str, dict]:
        workdir = Path(tempfile.mkdtemp(prefix="direct-worker-", dir=str(ROOT / "var")))
        self.addCleanup(lambda: __import__("shutil").rmtree(workdir, ignore_errors=True))
        config = {
            "kind": "simulate", "runId": "direct-worker", "project": project,
            "runSeed": 271828,
            "expectedVersions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        }
        if step_engines is not None:
            config["stepEngines"] = step_engines
        if device:
            config["selectedGpuDevice"] = DEVICE
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
        return completed.returncode, completed.stdout + completed.stderr, result

    def test_worker_direct_input_enforces_gpu_triangle_evidence_gate(self):
        project = self.gpu_triangle_project(2)
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        normalized["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
        code, output, result = self.run_direct_worker(normalized, device=False)
        self.assertNotEqual(code, 0)
        self.assertIn("GPU_TRIANGLE has no direct-reference acceptance row", output)
        self.assertIn("instead of falling back", output)
        self.assertNotIn("WORKER_COMPLETE", output)
        self.assertEqual(result, {})

    def test_worker_omitted_step_engines_inherits_explicit_gpu_triangle(self):
        project = self.gpu_triangle_project(2)
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        code, output, result = self.run_direct_worker(normalized, step_engines=None)
        if code != 0:
            if any(token in output for token in ("CUDA", "cuda", "GPU", "device")):
                self.skipTest(f"GPU unavailable in this sandbox: {output.strip().splitlines()[-1]}")
            self.fail(output)
        self.assertIn("WORKER_COMPLETE", output)
        self.assertEqual(result["actualSteps"][0]["engine"], "GPU_TRIANGLE")
        self.assertEqual(result["cudaDeviceReadback"]["uuid"], DEVICE["uuid"])

    def test_worker_pinned_engine_must_match_explicit_gpu_engine(self):
        project = self.gpu_triangle_project(2)
        parsed, _ = validate_project(project, kind="simulate")
        normalized = parsed.model_dump(mode="json", exclude_none=True)
        step_id = normalized["steps"][0]["id"]
        code, output, result = self.run_direct_worker(
            normalized, step_engines={step_id: "GPU_DISK"}, device=False)
        self.assertNotEqual(code, 0)
        self.assertIn("differs from solver.gpuEngine GPU_TRIANGLE", output)
        self.assertEqual(result, {})

    # ---------------------------------------------------------------------- Slice
    def slice_project(self, geometry: dict | None = None) -> dict:
        return project_v7(3, geometry or {
            "kind": "hole", "radius": 1.0, "depth": 1.0, "taperAngle": 0.0,
            "maskHeight": 0.5, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
            "maskMaterial": "Mask"}, name="native axis slice")

    def test_slice_default_request_hash_and_axis_validation(self):
        default = SliceRequestV7.model_validate({"position": 0.5})
        self.assertEqual(default.model_dump(mode="json", exclude_none=True), {"axis": 1, "position": 0.5})
        self.assertEqual(SliceRequestV7.model_validate({"position": 0.5, "sliceAxis": "x"})
                         .model_dump(mode="json", exclude_none=True)["sliceAxis"], "x")
        for bad in ({"position": 0.5, "sliceAxis": "h"}, {"position": 0.5, "sliceAxis": "X"},
                    {"position": 0.5, "axis": 0}, {"position": 0.5, "sliceAxis": 0},
                    {"sliceAxis": "x"}):
            with self.subTest(request=bad), self.assertRaises(Exception):
                SliceRequestV7.model_validate(bad)

    async def test_slice_default_request_hash_is_unchanged(self):
        project = self.slice_project()
        stripped = copy.deepcopy(project)
        stripped["steps"] = []
        base = requested_input_hash(parse_project(stripped))
        response = await asgi_request("/api/runs/slice", method="POST",
                                      payload={"project": project, "sliceRequest": {"position": 0.0}})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        result = await self.wait_run(run_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["sliceRequest"], {"axis": 1, "position": 0.0})
        expected = __import__("hashlib").sha256(
            (base + json.dumps({"axis": 1, "position": 0.0}, sort_keys=True,
                               separators=(",", ":"))).encode()).hexdigest()
        self.assertEqual(manifest["requestedInputHash"], expected)

    async def test_2d_project_rejects_slice_axes(self):
        two_d = project_v7(2, {"kind": "trench", "width": 1.5, "depth": 0.8, "taperAngle": 0.0,
                               "maskHeight": 0.5, "maskTaperAngle": 0.0,
                               "substrateMaterial": "Si", "maskMaterial": "Mask"}, name="2d slice")
        for axis in ("x", "y", "z"):
            response = await asgi_request("/api/runs/slice", method="POST",
                                          payload={"project": two_d,
                                                   "sliceRequest": {"sliceAxis": axis, "position": 0.0}})
            self.assertEqual(response[0], 422)
            detail = json.loads(response[2])["detail"]
            message = detail["message"] if isinstance(detail, dict) else str(detail)
            self.assertIn("3D", message)
            self.assertIn(f"{axis}-normal", message)

    async def test_slice_x_y_z_axis_native_coordinates_and_replay(self):
        project = self.slice_project()
        cases = [("x", 0.0, 0, [1.0, 0.0, 0.0], 8.0, True),
                 ("y", 0.0, 1, [0.0, 1.0, 0.0], 12.0, True),
                 ("z", -0.25, 2, [0.0, 0.0, 1.0], 12.0, False)]
        for axis, position, native_axis, normal, lateral_extent, full_span in cases:
            with self.subTest(axis=axis):
                request = {"sliceAxis": axis, "position": position}
                direct = await self.direct_reference(project, f"slice-{axis}", slice_request=request)
                response = await asgi_request("/api/runs/slice", method="POST",
                                              payload={"project": project, "sliceRequest": request})
                self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
                run_id = json_body(response)["runId"]
                result = await self.wait_run(run_id)
                self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
                snapshot = await self.snapshot(run_id, "true-slice")
                readback = snapshot["sliceReadback"]
                self.assertEqual(readback["axis"], native_axis)
                self.assertEqual(readback["axisName"], axis)
                self.assertEqual(readback["planeNormal"], normal)
                self.assertFalse(readback["reflectX"])
                self.assertFalse(readback["viewerClipping"])
                self.assertAlmostEqual(readback["outputLateralExtent"], lateral_extent)
                self.assertEqual(signature(snapshot), direct_signature(direct, "slice"))
                lateral = bounds(snapshot)["x"]
                vertical = bounds(snapshot)["h"]
                if full_span:
                    self.assertAlmostEqual(lateral[0], -lateral_extent / 2, places=9)
                    self.assertAlmostEqual(lateral[1], lateral_extent / 2, places=9)
                else:
                    self.assertGreaterEqual(lateral[0], -lateral_extent / 2 - 1e-9)
                    self.assertLessEqual(lateral[1], lateral_extent / 2 + 1e-9)
                    self.assertGreaterEqual(vertical[0], -lateral_extent / 2 - 1e-9)
                    self.assertLessEqual(vertical[1], lateral_extent / 2 + 1e-9)

                archive = await asgi_request(f"/api/runs/{run_id}/download")
                self.assertEqual(archive[0], 200)
                import io
                import zipfile
                with zipfile.ZipFile(io.BytesIO(archive[2])) as package:
                    replay = json.loads(package.read("replay-project.json"))
                self.assertEqual(replay["sliceRequest"], {"axis": 1, "position": position,
                                                          "sliceAxis": axis})
                replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(replay_response[0], 202,
                                 replay_response[2].decode("utf-8", errors="replace"))
                replay_id = json_body(replay_response)["runId"]
                replay_result = await self.wait_run(replay_id)
                self.assertEqual(replay_result["status"], "succeeded", replay_result.get("failureReason"))
                repeated = await self.snapshot(replay_id, "true-slice")
                self.assertEqual(signature(repeated), signature(snapshot))

    async def test_slice_reflect_x_mirrors_the_lateral_coordinate(self):
        project = self.slice_project({
            "kind": "trench", "width": 1.5, "depth": 0.8, "taperAngle": 0.0,
            "maskHeight": 0.5, "maskTaperAngle": 0.0, "halfTrench": True,
            "substrateMaterial": "Si", "maskMaterial": "Mask"})
        request = {"sliceAxis": "z", "position": -0.25, "reflectX": True}
        direct = await self.direct_reference(project, "slice-reflect", slice_request=request)
        response = await asgi_request("/api/runs/slice", method="POST",
                                      payload={"project": project, "sliceRequest": request})
        self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
        run_id = json_body(response)["runId"]
        result = await self.wait_run(run_id)
        self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        snapshot = await self.snapshot(run_id, "true-slice")
        self.assertTrue(snapshot["sliceReadback"]["reflectX"])
        self.assertEqual(signature(snapshot), direct_signature(direct, "slice"))
        self.assertLessEqual(bounds(snapshot)["x"][1], 0.0)

    async def test_slice_position_uses_normal_axis_extent(self):
        # Reproduces the swapped-bounds finding: X extent 2, Y extent 4,
        # grid 0.2. x-normal is limited by X (±1) even though its output plane
        # is Y-wide; y-normal is limited by Y (±2).
        project = project_v7(3, {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
                             name="axis bounds")
        project["domain"].update({"xExtent": 2.0, "yExtent": 4.0, "gridDelta": 0.2})
        for axis, position, expected in (("x", 0.8, 202), ("x", 1.5, 422),
                                         ("y", 1.5, 202), ("y", 2.5, 422)):
            with self.subTest(axis=axis, position=position):
                response = await asgi_request("/api/runs/slice", method="POST",
                                              payload={"project": project,
                                                       "sliceRequest": {"sliceAxis": axis,
                                                                        "position": position}})
                self.assertEqual(response[0], expected, response[2].decode("utf-8", errors="replace"))
                if response[0] == 202:
                    result = await self.wait_run(json_body(response)["runId"])
                    self.assertEqual(result["status"], "succeeded", result.get("failureReason"))

    async def test_slice_half_domain_positions_are_bounded(self):
        hole = project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0, "taperAngle": 0.0,
                              "maskHeight": 0.5, "maskTaperAngle": 0.0, "holeShape": "HALF",
                              "substrateMaterial": "Si", "maskMaterial": "Mask"},
                          name="half domain slice")
        hole["domain"].update({"xExtent": 8.0, "yExtent": 8.0, "gridDelta": 0.25})
        for position, expected in ((-0.2, 422), (0.25, 202)):
            with self.subTest(position=position):
                response = await asgi_request("/api/runs/slice", method="POST",
                                              payload={"project": hole,
                                                       "sliceRequest": {"sliceAxis": "x",
                                                                        "position": position}})
                self.assertEqual(response[0], expected, response[2].decode("utf-8", errors="replace"))
                if expected == 422:
                    detail = json.loads(response[2])["detail"]
                    self.assertIn("half-domain", detail["message"])
                else:
                    result = await self.wait_run(json_body(response)["runId"])
                    self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
        quarter = copy.deepcopy(hole)
        quarter["geometry"]["holeShape"] = "QUARTER"
        response = await asgi_request("/api/runs/slice", method="POST",
                                      payload={"project": quarter,
                                               "sliceRequest": {"sliceAxis": "y", "position": -0.25}})
        self.assertEqual(response[0], 422, response[2].decode("utf-8", errors="replace"))

    async def test_slice_extruded_fin_uses_actual_y_range(self):
        def fin_project(construction: str) -> dict:
            project = project_v7(3, {
                "kind": "fin", "width": 0.4, "height": 0.8, "taperAngle": 0.0,
                "maskHeight": 0.4, "maskTaperAngle": 0.0, "halfFin": True,
                "construction": construction,
                "extrusion": {"extent": [-1.0, 1.0], "axis": 1},
                "substrateMaterial": "Si", "maskMaterial": "Mask"},
                name=f"extruded half fin {construction}")
            project["domain"].update({"xExtent": 2.0, "yExtent": 4.0, "gridDelta": 0.2})
            return project

        from backend.validation import slice_axis_bounds
        for construction in ("native", "boolean"):
            project = fin_project(construction)
            bounds_y = slice_axis_bounds(project, {"sliceAxis": "y", "position": 0.5})
            self.assertEqual([bounds_y["min"], bounds_y["max"]], [-1.0, 1.0])
            with self.subTest(construction=construction, position=1.5):
                response = await asgi_request("/api/runs/slice", method="POST",
                                              payload={"project": project,
                                                       "sliceRequest": {"sliceAxis": "y",
                                                                        "position": 1.5}})
                self.assertEqual(response[0], 422, response[2].decode("utf-8", errors="replace"))
                detail = json.loads(response[2])["detail"]
                self.assertIn("extrusion limit", detail["message"])
            with self.subTest(construction=construction, position=0.5):
                response = await asgi_request("/api/runs/slice", method="POST",
                                              payload={"project": project,
                                                       "sliceRequest": {"sliceAxis": "y",
                                                                        "position": 0.5}})
                self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
                result = await self.wait_run(json_body(response)["runId"])
                self.assertEqual(result["status"], "succeeded", result.get("failureReason"))

    async def test_slice_metadata_uses_native_half_away_from_zero_rounding(self):
        project = project_v7(3, {"kind": "hole", "radius": 0.8, "depth": 0.4,
                                 "taperAngle": 0.0, "maskHeight": 0.4,
                                 "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                                 "maskMaterial": "Mask"}, name="rounding slice")
        project["domain"].update({"xExtent": 4.0, "yExtent": 4.0, "gridDelta": 0.2})
        references: dict[float, dict] = {}

        async def slice_at(position: float) -> dict:
            response = await asgi_request("/api/runs/slice", method="POST",
                                          payload={"project": project,
                                                   "sliceRequest": {"sliceAxis": "y",
                                                                    "position": position}})
            self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
            run_id = json_body(response)["runId"]
            result = await self.wait_run(run_id)
            self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
            return await self.snapshot(run_id, "true-slice")

        cases = [(0.09999999999999999, 0.0), (0.1, 0.2),
                 (0.0999999999999999, 0.0), (0.10000000000000003, 0.2),
                 (-0.1, -0.2), (-0.09999999999999999, -0.0)]
        for requested, expected in cases:
            with self.subTest(requested=requested, expected=expected):
                snapshot = await slice_at(requested)
                readback = snapshot["sliceReadback"]
                self.assertAlmostEqual(readback["requestedPosition"], requested, places=15)
                self.assertAlmostEqual(readback["position"], expected, places=15)
                self.assertTrue(readback["positionAdjustedToGrid"])
                if expected not in references:
                    references[expected] = await slice_at(expected)
                self.assertEqual(signature(snapshot), signature(references[expected]))

    async def test_preview_replay_document_keeps_a_valid_gpu_engine_pair(self):
        for engine in ("GPU_DISK", "GPU_TRIANGLE"):
            for with_steps in (False, True):
                with self.subTest(engine=engine, enabledSteps=with_steps):
                    project = self.gpu_triangle_project(2)
                    project["solver"]["gpuEngine"] = engine
                    if not with_steps:
                        project["steps"] = []
                    response = await asgi_request("/api/runs", method="POST",
                                                  payload={"kind": "preview", "project": project})
                    self.assertEqual(response[0], 202, response[2].decode("utf-8", errors="replace"))
                    run_id = json_body(response)["runId"]
                    result = await self.wait_run(run_id)
                    self.assertEqual(result["status"], "succeeded", result.get("failureReason"))
                    replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
                    self.assertEqual(replay["normalizedProject"]["solver"]["enginePreference"], "gpu")
                    self.assertEqual(replay["normalizedProject"]["solver"]["gpuEngine"], engine)
                    requirements = replay["executionRequirements"]["steps"]
                    if with_steps:
                        self.assertEqual(len(requirements), 1)
                        self.assertEqual(requirements[0]["engine"], engine)
                        self.assertEqual(requirements[0]["seed"],
                                         replay["normalizedProject"]["solver"]["random"]["seed"])
                    else:
                        self.assertEqual(requirements, [])
                    with patch("backend.supervisor.cuda_devices", return_value=GPU_INVENTORY):
                        replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                        self.assertEqual(replay_response[0], 202,
                                         replay_response[2].decode("utf-8", errors="replace"))
                        replay_id = json_body(replay_response)["runId"]
                        replay_result = await self.wait_run(replay_id)
                    if replay_result["status"] != "succeeded":
                        reason = str(replay_result.get("failureReason") or "")
                        if with_steps and any(token in reason for token in ("CUDA", "cuda", "GPU", "device")):
                            self.skipTest(f"GPU unavailable in this sandbox: {reason.strip()}")
                        self.fail(reason)
                    if with_steps:
                        manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
                        self.assertEqual(manifest["actualExecution"][-1]["actualEngine"], engine)

    # ------------------------------------------------------------- Hole/fin/trench
    def test_omitted_native_shape_flags_keep_historical_hashes(self):
        expected = {
            "hole3d": "c32664968a9d65dc140104d0487a63c7d4d087ea053c9a3a3af124cd63ec2b1d",
            "trench2d": "f8b9ae164287468f83e4d3ebf7750e45dca8e745f7d83b24d10bc94456364b6f",
            "trench3d": "447dfda7b027769f14ba146984f087f8ad7e18768bf2776f0bc1802d7e9a4898",
            "fin2d": "8a662239d60c58606db67e168036d5f24e144539184a51fce617fb7ba03cfc21",
            "fin3d": "271f0b37df926db579668d73eff6485e5e4b2a75ffc7f610bdffb561bac4d5d6",
        }
        geometries = {
            "hole3d": (3, {"kind": "hole", "radius": 1.0, "depth": 1.0, "taperAngle": 0.0,
                           "maskHeight": 0.4, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                           "maskMaterial": "Mask"}),
            "trench2d": (2, {"kind": "trench", "width": 1.5, "depth": 0.8, "taperAngle": 0.0,
                             "maskHeight": 0.3, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                             "maskMaterial": "Mask"}),
            "trench3d": (3, {"kind": "trench", "width": 1.5, "depth": 0.8, "taperAngle": 0.0,
                             "maskHeight": 0.3, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                             "maskMaterial": "Mask"}),
            "fin2d": (2, {"kind": "fin", "width": 0.8, "height": 1.0, "taperAngle": 0.0,
                          "maskHeight": 0.5, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                          "maskMaterial": "Mask"}),
            "fin3d": (3, {"kind": "fin", "width": 0.8, "height": 1.0, "taperAngle": 0.0,
                          "maskHeight": 0.5, "maskTaperAngle": 0.0, "substrateMaterial": "Si",
                          "maskMaterial": "Mask"}),
        }
        for name, (dimension, geometry) in geometries.items():
            with self.subTest(case=name):
                project = project_v7(dimension, geometry, name=name)
                parsed = parse_project(project)
                self.assertEqual(requested_input_hash(parsed), expected[name])
                self.assertFalse({"holeShape", "halfTrench", "halfFin"} & set(parsed.geometry))

    def test_invalid_half_shape_combinations_are_rejected(self):
        cases = [
            ("periodic hole HALF", project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                                   "taperAngle": 0.0, "maskHeight": 0.5,
                                                   "maskTaperAngle": 0.0, "holeShape": "HALF",
                                                   "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                               name="periodic half hole"), "periodic"),
            ("periodic hole QUARTER", project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                                      "taperAngle": 0.0, "maskHeight": 0.5,
                                                      "maskTaperAngle": 0.0, "holeShape": "QUARTER",
                                                      "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                                  name="periodic quarter hole"), "periodic"),
            ("periodic halfTrench", project_v7(2, {"kind": "trench", "width": 1.5, "depth": 0.8,
                                                    "taperAngle": 0.0, "maskHeight": 0.5,
                                                    "maskTaperAngle": 0.0, "halfTrench": True,
                                                    "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                                name="periodic half trench"), "periodic"),
            ("periodic halfFin", project_v7(2, {"kind": "fin", "width": 0.8, "height": 1.0,
                                                 "taperAngle": 0.0, "maskHeight": 0.5,
                                                 "maskTaperAngle": 0.0, "halfFin": True,
                                                 "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                             name="periodic half fin"), "periodic"),
            ("2D hole HALF", project_v7(2, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                             "taperAngle": 0.0, "maskHeight": 0.5,
                                             "maskTaperAngle": 0.0, "holeShape": "HALF",
                                             "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                         name="2d half hole"), "3D"),
        ]
        for name, project, needle in cases:
            if name.endswith("periodic hole HALF") or name.endswith("periodic hole QUARTER"):
                project["domain"]["boundaryXY"] = "periodic"
            elif name.startswith("periodic"):
                project["domain"]["xBoundary"] = "periodic"
            with self.subTest(case=name):
                with self.assertRaises(ProjectInputError) as context:
                    validate_project(project, kind="simulate")
                messages = " ".join(issue.message for issue in context.exception.issues)
                self.assertIn(needle, messages)
                if name != "2D hole HALF":
                    self.assertIn("silently", messages)

        with self.assertRaises(ProjectInputError) as context:
            validate_project(project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                            "taperAngle": 0.0, "maskHeight": 0.5,
                                            "maskTaperAngle": 0.0, "holeShape": "half",
                                            "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                        name="bad shape"), kind="simulate")
        self.assertTrue(any("holeShape" in issue.path for issue in context.exception.issues))

    async def half_geometry_cases(self) -> list[tuple[str, dict]]:
        return [
            ("trench2d-half", project_v7(2, {"kind": "trench", "width": 1.5, "depth": 0.8,
                                              "taperAngle": 0.0, "maskHeight": 0.5,
                                              "maskTaperAngle": 0.0, "halfTrench": True,
                                              "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                          name="half trench 2d")),
            ("trench3d-half", project_v7(3, {"kind": "trench", "width": 1.5, "depth": 0.8,
                                              "taperAngle": 0.0, "maskHeight": 0.5,
                                              "maskTaperAngle": 0.0, "halfTrench": True,
                                              "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                          name="half trench 3d")),
            ("fin2d-half", project_v7(2, {"kind": "fin", "width": 0.8, "height": 1.0,
                                           "taperAngle": 0.0, "maskHeight": 0.5,
                                           "maskTaperAngle": 0.0, "halfFin": True,
                                           "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                       name="half fin 2d")),
            ("fin3d-half", project_v7(3, {"kind": "fin", "width": 0.8, "height": 1.0,
                                           "taperAngle": 0.0, "maskHeight": 0.5,
                                           "maskTaperAngle": 0.0, "halfFin": True,
                                           "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                       name="half fin 3d")),
            ("hole3d-half", project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                            "taperAngle": 0.0, "maskHeight": 0.5,
                                            "maskTaperAngle": 0.0, "holeShape": "HALF",
                                            "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                        name="half hole 3d")),
            ("hole3d-quarter", project_v7(3, {"kind": "hole", "radius": 1.0, "depth": 1.0,
                                               "taperAngle": 0.0, "maskHeight": 0.5,
                                               "maskTaperAngle": 0.0, "holeShape": "QUARTER",
                                               "substrateMaterial": "Si", "maskMaterial": "Mask"},
                                           name="quarter hole 3d")),
        ]

    async def test_boolean_fin_route_keeps_half_fin(self):
        for dimension in (2, 3):
            project = project_v7(dimension, {
                "kind": "fin", "width": 0.8, "height": 1.0, "taperAngle": 0.0,
                "maskHeight": 0.5, "maskTaperAngle": 0.0, "construction": "boolean",
                "halfFin": True, "substrateMaterial": "Si", "maskMaterial": "Mask"},
                name=f"boolean half fin {dimension}d")
            run_id, _ = await self.run_preview(project)
            snapshot = await self.snapshot(run_id, "initial")
            readback = snapshot["geometryReadback"]
            self.assertEqual(readback["construction"], "boolean")
            self.assertTrue(readback["arguments"]["halfFin"])
            self.assertGreaterEqual(bounds(snapshot)["x"][0], -1e-9)
            self.assertLessEqual(bounds(snapshot)["x"][1], project["domain"]["xExtent"] / 2 + 1e-9)

    async def test_native_half_shapes_product_direct_symmetry_and_replay(self):
        for name, project in await self.half_geometry_cases():
            with self.subTest(case=name):
                direct = await self.direct_reference(project, name)
                run_id, result = await self.run_preview(project)
                snapshot = await self.snapshot(run_id, "initial")
                self.assertEqual(signature(snapshot), direct_signature(direct, "initial"))
                readback = snapshot["geometryReadback"]
                geometry = project["geometry"]
                if geometry["kind"] == "hole":
                    self.assertEqual(readback["actualHoleShape"], geometry["holeShape"])
                elif geometry["kind"] == "trench":
                    self.assertTrue(readback["actualHalfTrench"])
                else:
                    self.assertTrue(readback["arguments"]["halfFin"])
                lateral = bounds(snapshot)
                self.assertGreaterEqual(lateral["x"][0], -1e-9)
                if geometry.get("holeShape") == "QUARTER":
                    self.assertGreaterEqual(lateral["h"][0], -1e-9)
                if geometry["kind"] in ("hole", "trench"):
                    replay_response = await asgi_request(f"/api/runs/{run_id}/replay-project")
                    self.assertEqual(replay_response[0], 200)
                    replay = json_body(replay_response)
                    start = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                    self.assertEqual(start[0], 202, start[2].decode("utf-8", errors="replace"))
                    replay_id = json_body(start)["runId"]
                    replay_result = await self.wait_run(replay_id)
                    self.assertEqual(replay_result["status"], "succeeded", replay_result.get("failureReason"))
                    repeated = await self.snapshot(replay_id, "initial")
                    self.assertEqual(signature(repeated), signature(snapshot))


if __name__ == "__main__":
    unittest.main()
