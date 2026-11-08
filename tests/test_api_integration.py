from __future__ import annotations

import asyncio
import io
import json
import math
import os
import subprocess
import sys
import time
import unittest
import zipfile
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from backend.models import Project  # noqa: E402
from backend import storage  # noqa: E402
from backend.supervisor import Supervisor, WORKER as REAL_WORKER  # noqa: E402
from backend.runtime_limits import available_cpu_threads  # noqa: E402


def default_project() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "name": "API 集成验收",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.1, "xExtent": 5.0, "xBoundary": "reflective"},
        "geometry": {"kind": "trench", "width": 2.0, "depth": 2.2, "taperAngle": 0.0,
                     "maskHeight": 0.0, "maskTaperAngle": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 64, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159},
                   "enginePreference": "auto", "timeoutSeconds": 30},
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def plane_project(name: str) -> dict[str, Any]:
    project = default_project()
    project["name"] = name
    project["domain"] = {"gridDelta": 0.1, "xExtent": 4.0, "xBoundary": "reflective"}
    project["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    return project


async def asgi_request(
    path: str,
    *,
    method: str = "GET",
    payload: Any = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    include_content_length: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    body = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))
    parsed_path = urlsplit(path)
    asgi_path = parsed_path.path or "/"
    request_headers = [(b"host", b"127.0.0.1:8767")]
    if payload is not None:
        request_headers.append((b"content-type", b"application/json"))
        if include_content_length:
            request_headers.append((b"content-length", str(len(body)).encode("ascii")))
    if headers:
        for key, value in headers:
            request_headers = [(existing_key, existing_value) for existing_key, existing_value in request_headers if existing_key.lower() != key.lower()]
            request_headers.append((key, value))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method, "scheme": "http", "path": asgi_path,
        "raw_path": asgi_path.encode("ascii"), "query_string": parsed_path.query.encode("ascii"), "root_path": "",
        "headers": request_headers, "client": ("127.0.0.1", 32123),
        "server": ("127.0.0.1", 8767), "app": app,
    }
    delivered = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            await asyncio.Future()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in start["headers"]}
    return int(start["status"]), response_headers, response_body


def json_body(response: tuple[int, dict[str, str], bytes]) -> dict[str, Any]:
    return json.loads(response[2].decode("utf-8"))


def point_segment_distance(point: list[float], first: list[float], second: list[float]) -> float:
    dx = second[0] - first[0]
    dh = second[1] - first[1]
    denominator = dx * dx + dh * dh
    if denominator == 0:
        return ((point[0] - first[0]) ** 2 + (point[1] - first[1]) ** 2) ** 0.5
    weight = max(0.0, min(1.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dh) / denominator))
    nearest_x = first[0] + weight * dx
    nearest_h = first[1] + weight * dh
    return ((point[0] - nearest_x) ** 2 + (point[1] - nearest_h) ** 2) ** 0.5


def mesh_distance(first: dict[str, Any], second: dict[str, Any]) -> float:
    def directed(points: list[list[float]], other: dict[str, Any]) -> float:
        segments = [(other["points"][a], other["points"][b]) for a, b in other["lines"]]
        return max(min(point_segment_distance(point, a, b) for a, b in segments) for point in points)
    return max(directed(first["points"], second), directed(second["points"], first))


class ApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        (storage.VAR / "probe-tests").mkdir(parents=True, exist_ok=True)
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def wait_for_terminal(self, run_id: str, timeout: float = 40.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/runs/{run_id}")
            self.assertEqual(response[0], 200, response[2].decode("utf-8", errors="replace"))
            data = json_body(response)
            if data["status"] in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
                return data
            await asyncio.sleep(0.1)
        self.fail(f"run {run_id} did not reach a terminal state within {timeout} seconds")

    async def test_cpu_thread_limit_is_dynamic_and_advertised(self) -> None:
        maximum = available_cpu_threads()
        capability_response = await asgi_request("/api/capabilities")
        self.assertEqual(capability_response[0], 200)
        cpu_policy = json_body(capability_response)["resourcePolicy"]["run"]["cpuThreads"]
        self.assertEqual(cpu_policy["maximum"], maximum)
        self.assertIn(cpu_policy["limitSource"], {"process CPU affinity", "os.cpu_count fallback"})

        accepted = default_project()
        accepted["solver"]["cpuThreads"] = maximum
        response = await asgi_request("/api/projects/validate", method="POST", payload=accepted)
        self.assertEqual(response[0], 200, response[2].decode("utf-8", errors="replace"))

        rejected = json.loads(json.dumps(accepted))
        rejected["solver"]["cpuThreads"] = maximum + 1
        response = await asgi_request("/api/projects/validate", method="POST", payload=rejected)
        self.assertEqual(response[0], 422)
        self.assertIn("solver.cpuThreads", {item["path"] for item in json_body(response)["detail"]})

    async def test_project_api_preview_and_bounded_archive(self) -> None:
        project = default_project()
        validate = await asgi_request("/api/projects/validate", method="POST", payload=project)
        self.assertEqual(validate[0], 200, validate[2].decode("utf-8", errors="replace"))
        self.assertTrue(json_body(validate)["valid"])

        created = await asgi_request("/api/projects", method="POST", payload=project)
        self.assertEqual(created[0], 201, created[2].decode("utf-8", errors="replace"))
        project_record = json_body(created)
        project_id = project_record["id"]
        loaded = await asgi_request(f"/api/projects/{project_id}")
        self.assertEqual(loaded[0], 200)
        self.assertEqual(json_body(loaded)["project"], project)
        updated = await asgi_request(f"/api/projects/{project_id}", method="PUT", payload=project)
        self.assertEqual(updated[0], 200)
        listed = json_body(await asgi_request("/api/projects"))["projects"]
        self.assertIn(project_id, {row["id"] for row in listed})

        rejected_host = await asgi_request("/api/health", headers=[(b"host", b"example.com")])
        self.assertEqual(rejected_host[0], 400)
        rejected_origin = await asgi_request("/api/health", headers=[(b"origin", b"https://example.com")])
        self.assertEqual(rejected_origin[0], 403)
        # General JSON bodies have no fixed byte cap; an oversized invalid body
        # is rejected by project validation (422), not by the middleware.
        too_large = await asgi_request("/api/projects/validate", method="POST", payload=b"x" * (1024 * 1024 + 1), include_content_length=False)
        self.assertEqual(too_large[0], 422)
        # The dedicated GDS upload cap still returns 413.
        with patch("backend.main.API_GDS_MAX_BODY", 4):
            gds_too_large = await asgi_request(
                "/api/gds-assets?file_name=oversized.gds", method="POST", payload=b"x" * 8,
                headers=[(b"content-type", b"application/octet-stream")])
        self.assertEqual(gds_too_large[0], 413)

        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "preview", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        completed = await self.wait_for_terminal(run_id)
        self.assertEqual(completed["status"], "succeeded", completed.get("failureReason"))
        self.assertEqual(completed["snapshots"][0]["stepId"], "initial")

        snapshot_response = await asgi_request(f"/api/runs/{run_id}/snapshots/initial")
        self.assertEqual(snapshot_response[0], 200)
        snapshot = json_body(snapshot_response)
        self.assertEqual(snapshot["axisMap"], {"x": 0, "h": 1, "z": 2})
        self.assertAlmostEqual(snapshot["gridDelta"], project["domain"]["gridDelta"], places=12)
        self.assertTrue(snapshot["points"])
        self.assertEqual(len(snapshot["lines"]), len(snapshot["cellData"]["materialIds"]))

        refined_project = json.loads(json.dumps(project))
        refined_project["domain"]["gridDelta"] = 0.05
        refined = await asgi_request("/api/runs", method="POST", payload={"kind": "preview", "project": refined_project})
        self.assertEqual(refined[0], 202, refined[2].decode("utf-8", errors="replace"))
        refined_id = json_body(refined)["runId"]
        self.assertNotEqual(refined_id, run_id)
        refined_status = await self.wait_for_terminal(refined_id)
        self.assertEqual(refined_status["status"], "succeeded", refined_status.get("failureReason"))
        old_snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/initial"))
        new_snapshot = json_body(await asgi_request(f"/api/runs/{refined_id}/snapshots/initial"))
        old_manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        new_manifest = storage.read_json(storage.run_path(refined_id) / "manifest.json")
        self.assertAlmostEqual(old_snapshot["gridDelta"], 0.1, places=12)
        self.assertAlmostEqual(new_snapshot["gridDelta"], 0.05, places=12)
        self.assertAlmostEqual(new_manifest["workerResult"]["gridDeltaReadback"], 0.05, places=12)
        self.assertNotEqual(old_manifest["requestedInputHash"], new_manifest["requestedInputHash"])

        archive_response = await asgi_request(f"/api/runs/{run_id}/download")
        self.assertEqual(archive_response[0], 200)
        with zipfile.ZipFile(io.BytesIO(archive_response[2])) as archive:
            self.assertIsNone(archive.testzip())
            self.assertTrue({"project.json", "replay-project.json", "manifest.json", "initial.json"}.issubset(archive.namelist()))
            self.assertNotIn("initial.vtk", archive.namelist())

    async def test_schema_units_model_and_resource_validation_paths(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = []
        project = default_project()
        project["schemaVersion"] = 99
        cases.append(("future schema", project, "schemaVersion"))
        project = default_project()
        project["units"]["length"] = "mm"
        cases.append(("unsupported unit", project, "units.length"))
        project = default_project()
        project["domain"]["gridDelta"] = True
        cases.append(("boolean numeric", project, "domain.gridDelta"))
        project = default_project()
        project["unexpected"] = 1
        cases.append(("unknown field", project, "unexpected"))
        project = default_project()
        project["steps"] = [{"id": "step_unknown_01", "name": "invalid", "enabled": True, "operation": "etch",
                             "model": "plasma", "rateMagnitude": 0.1, "duration": 1.0}]
        cases.append(("unknown model", project, "steps[0].model"))
        for label, payload, expected_path in cases:
            response = await asgi_request("/api/projects/validate", method="POST", payload=payload)
            self.assertEqual(response[0], 422, label)
            paths = {item["path"] for item in json_body(response)["detail"]}
            self.assertIn(expected_path, paths, label)

        unbounded_project = default_project()
        unbounded_project["steps"] = [{"id": f"step_{index:05d}", "name": "step", "enabled": False, "operation": "etch",
                                       "model": "isotropic", "rateMagnitude": 0.0, "duration": 0.1} for index in range(21)]
        response = await asgi_request("/api/projects/validate", method="POST", payload=unbounded_project)
        self.assertEqual(response[0], 200, json_body(response))
        self.assertEqual(len(json_body(response)["normalizedProject"]["steps"]), 21)

        high_resolution = default_project()
        high_resolution["domain"] = {"gridDelta": 0.005, "xExtent": 40.0, "xBoundary": "reflective"}
        high_resolution["solver"]["raysPerPoint"] = 4096
        high_resolution["steps"] = [{"id": "step_particle_hr", "name": "high resolution particle", "enabled": True,
            "operation": "etch", "model": "singleParticle", "rateMagnitude": 0.1, "duration": 0.1,
            "stickingProbability": 0.5, "sourceExponent": 1.0}]
        response = await asgi_request("/api/projects/validate", method="POST", payload=high_resolution)
        self.assertEqual(response[0], 200, json_body(response))

    async def test_isotropic_displacement_and_material_mapping(self) -> None:
        for operation, expected_height, material_id in (("etch", -0.2, 10), ("deposit", 0.2, 30)):
            project = plane_project(f"各向同性{operation}验收")
            project["solver"]["enginePreference"] = "gpu"
            step = {"id": f"step_{operation}_001", "name": operation, "enabled": True,
                    "operation": operation, "model": "isotropic", "rateMagnitude": 0.5, "duration": 0.4}
            if operation == "deposit":
                step["depositMaterial"] = "SiO2"
            project["steps"] = [step]
            submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
            self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
            run_id = json_body(submitted)["runId"]
            status = await self.wait_for_terminal(run_id)
            self.assertEqual(status["status"], "succeeded", status.get("failureReason"))
            snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{step['id']}"))
            target_points = [point[1] for point in snapshot["points"]]
            if operation == "etch":
                measured = sorted(target_points)[len(target_points) // 2]
            else:
                material_lines = [line for line, mid in zip(snapshot["lines"], snapshot["cellData"]["materialIds"]) if mid == material_id]
                measured = max(snapshot["points"][index][1] for line in material_lines for index in line)
            self.assertLessEqual(abs(measured - expected_height), 2 * project["domain"]["gridDelta"] + 1e-9)
            self.assertIn(material_id, snapshot["cellData"]["materialIds"])
            manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
            engine_record = manifest["actualExecution"][0]
            self.assertEqual(engine_record["actualEngine"], "not-applicable")
            self.assertEqual(engine_record["requestedEnginePreference"], "gpu")

    async def test_two_deposits_etch_and_zero_rate_snapshot(self) -> None:
        project = plane_project("连续沉积刻蚀与 no-op 验收")
        project["steps"] = [
            {"id": "step_dep1_001", "name": "第一次 SiO2 沉积", "enabled": True, "operation": "deposit", "model": "isotropic", "rateMagnitude": 0.5, "duration": 0.2, "depositMaterial": "SiO2"},
            {"id": "step_dep2_001", "name": "第二次 SiO2 沉积", "enabled": True, "operation": "deposit", "model": "isotropic", "rateMagnitude": 0.5, "duration": 0.2, "depositMaterial": "SiO2"},
            {"id": "step_noop_001", "name": "零速率 no-op", "enabled": True, "operation": "etch", "model": "singleParticle", "rateMagnitude": 0.0, "duration": 0.2, "stickingProbability": 0.5, "sourceExponent": 1.0},
            {"id": "step_etch_001", "name": "SiO2 刻蚀", "enabled": True, "operation": "etch", "model": "isotropic", "rateMagnitude": 0.5, "duration": 0.2},
        ]
        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        status = await self.wait_for_terminal(run_id)
        self.assertEqual(status["status"], "succeeded", status.get("failureReason"))
        snapshots = {}
        for step in project["steps"]:
            response = await asgi_request(f"/api/runs/{run_id}/snapshots/{step['id']}")
            self.assertEqual(response[0], 200)
            snapshots[step["id"]] = json_body(response)
        self.assertEqual(snapshots["step_dep2_001"]["materialIdsByLevelSetIndex"], [10, 30, 30])
        before_noop = snapshots["step_dep2_001"]
        visible_sio2_heights = sorted({
            round((before_noop["points"][line[0]][1] + before_noop["points"][line[1]][1]) / 2, 6)
            for line, material_id in zip(before_noop["lines"], before_noop["cellData"]["materialIds"])
            if material_id == 30 and abs((before_noop["points"][line[0]][0] + before_noop["points"][line[1]][0]) / 2) < 0.11
        })
        self.assertEqual(visible_sio2_heights, [0.2], "same-material internal deposition interface must not be rendered")
        self.assertTrue(before_noop["sameMaterialInternalInterfacesHidden"])
        after_noop = snapshots["step_noop_001"]
        for key in ("points", "lines", "cellData", "materials", "materialIdsByLevelSetIndex", "sameMaterialInternalInterfacesHidden"):
            self.assertEqual(after_noop[key], before_noop[key], f"zero-rate no-op changed {key}")
        self.assertTrue(after_noop["noOp"])
        self.assertFalse((storage.run_path(run_id) / "step_dep2_001.vtk").exists())
        self.assertFalse((storage.run_path(run_id) / "step_noop_001.vtk").exists())
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        no_op_record = next(item for item in manifest["actualExecution"] if item["stepId"] == "step_noop_001")
        self.assertEqual(no_op_record["actualEngine"], "not-applicable")
        self.assertEqual(no_op_record["actualParameters"], {})
        self.assertTrue(no_op_record["noOp"])

    async def test_gpu_rejection_lock_concurrency_latency_and_cancel_reap(self) -> None:
        project = plane_project("取消与并发验收")
        project["steps"] = [{"id": "step_particle_02", "name": "粒子过程", "enabled": True, "operation": "etch",
                             "model": "singleParticle", "rateMagnitude": 0.2, "duration": 0.2,
                             "stickingProbability": 0.5, "sourceExponent": 1.0}]
        gpu_project = json.loads(json.dumps(project))
        gpu_project["solver"]["enginePreference"] = "gpu"
        validated_gpu = await asgi_request("/api/projects/validate", method="POST", payload=gpu_project)
        self.assertEqual(validated_gpu[0], 200)
        self.assertTrue(json_body(validated_gpu)["valid"])
        empty_simulate = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": plane_project("空配方")})
        self.assertEqual(empty_simulate[0], 422)

        with patch("backend.supervisor.WORKER", REAL_WORKER.parent.parent / "tests" / "fixtures" / "sleep_worker.py"):
            submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
            self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
            run_id = json_body(submitted)["runId"]
            duplicate = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
            self.assertEqual(duplicate[0], 409)
            self.assertEqual(json_body(duplicate)["detail"]["runId"], run_id)
            second_instance = subprocess.run(
                [str(REAL_WORKER.parent.parent / ".venv" / "bin" / "python"), "-c",
                 "from backend.storage import acquire_instance_lock; fd=acquire_instance_lock(); print('ACQUIRED')"],
                cwd=REAL_WORKER.parent.parent, capture_output=True, text=True, timeout=3,
            )
            self.assertNotEqual(second_instance.returncode, 0, second_instance.stdout + second_instance.stderr)
            self.assertNotIn("ACQUIRED", second_instance.stdout)

            deadline = asyncio.get_running_loop().time() + 5
            while asyncio.get_running_loop().time() < deadline:
                current = await asgi_request(f"/api/runs/{run_id}")
                if json_body(current)["status"] == "running":
                    break
                await asyncio.sleep(0.05)
            start = time.perf_counter()
            health = await asgi_request("/api/health")
            current = await asgi_request(f"/api/runs/{run_id}")
            cancel = await asgi_request(f"/api/runs/{run_id}/cancel", method="POST")
            latency = time.perf_counter() - start
            self.assertEqual(health[0], 200)
            self.assertEqual(current[0], 200)
            self.assertIn(json_body(cancel)["status"], {"cancelling", "cancelled"})
            self.assertLess(latency, 2.0)
            cancelled_at = time.perf_counter()
            status = await self.wait_for_terminal(run_id, timeout=5)
            self.assertLess(time.perf_counter() - cancelled_at, 5.0)
            self.assertEqual(status["status"], "cancelled")
            manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
            self.assertEqual(manifest["childRecord"]["terminationReason"], "supervisor_cancelled")
            self.assertEqual(manifest["childRecord"]["remainingOwnedGroupMembers"], [])

    @unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires the host GPU_DISK capability probe")
    async def test_verified_gpu_disk_run_and_replay(self) -> None:
        project = plane_project("GPU_DISK 引擎验收")
        project["domain"]["gridDelta"] = 0.2
        project["steps"] = [{"id": "step_gpu_0001", "name": "单粒子刻蚀", "enabled": True, "operation": "etch",
                             "model": "singleParticle", "rateMagnitude": 0.2, "duration": 0.2,
                             "stickingProbability": 0.5, "sourceExponent": 1.0}]

        cpu_project = json.loads(json.dumps(project))
        cpu_project["solver"]["enginePreference"] = "cpu"
        cpu_submit = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": cpu_project})
        self.assertEqual(cpu_submit[0], 202, cpu_submit[2].decode("utf-8", errors="replace"))
        cpu_id = json_body(cpu_submit)["runId"]
        cpu_status = await self.wait_for_terminal(cpu_id, timeout=90)
        self.assertEqual(cpu_status["status"], "succeeded", cpu_status.get("failureReason"))
        cpu_record = storage.read_json(storage.run_path(cpu_id) / "manifest.json")["actualExecution"][0]
        self.assertEqual(cpu_record["actualEngine"], "CPU_DISK")

        gpu_project = json.loads(json.dumps(project))
        gpu_project["solver"]["enginePreference"] = "gpu"
        gpu_submit = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": gpu_project})
        self.assertEqual(gpu_submit[0], 202, gpu_submit[2].decode("utf-8", errors="replace"))
        gpu_id = json_body(gpu_submit)["runId"]
        gpu_status = await self.wait_for_terminal(gpu_id, timeout=90)
        self.assertEqual(gpu_status["status"], "succeeded", gpu_status.get("failureReason"))
        gpu_manifest = storage.read_json(storage.run_path(gpu_id) / "manifest.json")
        gpu_record = gpu_manifest["actualExecution"][0]
        self.assertEqual(gpu_record["actualEngine"], "GPU_DISK")
        self.assertEqual(gpu_record["requestedEnginePreference"], "gpu")

        replay_response = await asgi_request(f"/api/runs/{gpu_id}/replay-project")
        self.assertEqual(replay_response[0], 200)
        replay = json_body(replay_response)
        self.assertEqual(replay["normalizedProject"]["solver"]["enginePreference"], "gpu")
        self.assertEqual(replay["executionRequirements"]["steps"][0]["engine"], "GPU_DISK")
        replay_submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replay_submit[0], 202, replay_submit[2].decode("utf-8", errors="replace"))
        replay_id = json_body(replay_submit)["runId"]
        replay_status = await self.wait_for_terminal(replay_id, timeout=90)
        self.assertEqual(replay_status["status"], "succeeded", replay_status.get("failureReason"))
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        self.assertEqual(replay_manifest["actualExecution"][0]["actualEngine"], "GPU_DISK")
        self.assertEqual(replay_manifest["actualSeed"], gpu_manifest["actualSeed"])
        gpu_snapshot = json_body(await asgi_request(f"/api/runs/{gpu_id}/snapshots/step_gpu_0001"))
        replay_snapshot = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/step_gpu_0001"))
        self.assertEqual(set(gpu_snapshot["cellData"]["materialIds"]), set(replay_snapshot["cellData"]["materialIds"]))
        self.assertLessEqual(mesh_distance(gpu_snapshot, replay_snapshot), 0.4 + 1e-9)

    @unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires host GPU_DISK evidence")
    async def test_verified_gpu_3d_run_and_replay_keeps_engine_and_mesh(self) -> None:
        project_path = Path(__file__).resolve().parent / "fixtures" / "project-3d.json"
        project = json.loads(project_path.read_text(encoding="utf-8"))
        project["name"] = "Stage 9 3D GPU_DISK"
        project["steps"] = [project["steps"][0]]
        project["solver"]["enginePreference"] = "gpu"
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)

        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        completed = await self.wait_for_terminal(run_id, timeout=90)
        self.assertEqual(completed["status"], "succeeded", completed.get("failureReason"))
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["actualExecution"][0]["actualEngine"], "GPU_DISK")
        self.assertEqual(manifest["actualExecution"][0]["requestedEnginePreference"], "gpu")
        original = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/step_deposit"))
        self.assertIn(30, original["cellData"]["materialIds"])

        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["executionRequirements"]["steps"][0]["engine"], "GPU_DISK")
        replay_submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replay_submit[0], 202, replay_submit[2].decode("utf-8", errors="replace"))
        replay_id = json_body(replay_submit)["runId"]
        replay_status = await self.wait_for_terminal(replay_id, timeout=90)
        self.assertEqual(replay_status["status"], "succeeded", replay_status.get("failureReason"))
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        self.assertEqual(replay_manifest["actualExecution"][0]["actualEngine"], "GPU_DISK")
        replay_mesh = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/step_deposit"))
        for key in ("points", "triangles", "cellData", "materialIdsByLevelSetIndex"):
            self.assertEqual(original[key], replay_mesh[key], key)

    @unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires host GPU_DISK evidence")
    async def test_schema7_specialized_gpu_run_history_and_replay_match_direct_vienna(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
        from stage8_project_factory import make_project

        project = make_project("sf6o2", 2)
        project["solver"]["enginePreference"] = "gpu"
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)
        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        completed = await self.wait_for_terminal(run_id, timeout=90)
        self.assertEqual(completed["status"], "succeeded", completed.get("failureReason"))
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        execution = manifest["actualExecution"][0]
        self.assertEqual(execution["actualEngine"], "GPU_DISK")
        self.assertEqual(execution["requestedEnginePreference"], "gpu")
        self.assertEqual(execution["actualParameters"]["nativeModel"]["nativeProcessEngine"], "GPU_DISK")

        matrix = storage.read_json(REAL_WORKER.parent.parent / "docs" / "stage9-evidence" / "gpu-capability-matrix.json")
        capability = next(row for row in matrix["rows"] if row["model"] == "specialized:sf6o2" and row["dimension"] == 2)
        direct = storage.read_json(REAL_WORKER.parent.parent / capability["evidence"]["directReference"])
        snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{project['steps'][0]['id']}"))
        direct_final = direct["final"]
        self.assertEqual(snapshot["points"], direct_final["points"])
        self.assertEqual(snapshot["lines"], direct_final["cells"])
        self.assertEqual(snapshot["cellData"]["materialIds"], direct_final["materialIds"])
        self.assertEqual(snapshot["materialIdsByLevelSetIndex"], direct_final["materialIdsByLevelSetIndex"])

        history = json_body(await asgi_request(f"/api/runs/{run_id}"))
        self.assertEqual(history["actualExecution"][0]["actualEngine"], "GPU_DISK")
        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        replay_submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replay_submit[0], 202, replay_submit[2].decode("utf-8", errors="replace"))
        replay_id = json_body(replay_submit)["runId"]
        replay_status = await self.wait_for_terminal(replay_id, timeout=90)
        self.assertEqual(replay_status["status"], "succeeded", replay_status.get("failureReason"))
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        self.assertEqual(replay_manifest["actualExecution"][0]["actualEngine"], "GPU_DISK")
        replay_snapshot = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/{project['steps'][0]['id']}"))
        for key in ("points", "lines", "cellData", "materialIdsByLevelSetIndex"):
            self.assertEqual(snapshot[key], replay_snapshot[key], key)

    @unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires host GPU_DISK evidence")
    async def test_schema7_multiparticle_gpu_run_rate_callback_and_replay_match_direct_vienna(self) -> None:
        from tests.test_stage7 import multi_project

        project = multi_project(2, "etch")
        project["solver"]["enginePreference"] = "gpu"
        validation = json_body(await asgi_request("/api/projects/validate", method="POST", payload=project))
        self.assertTrue(validation["valid"], validation)
        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        completed = await self.wait_for_terminal(run_id, timeout=90)
        self.assertEqual(completed["status"], "succeeded", completed.get("failureReason"))
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        execution = manifest["actualExecution"][0]
        self.assertEqual(execution["actualEngine"], "GPU_DISK")
        self.assertEqual(execution["requestedEnginePreference"], "gpu")
        self.assertGreater(execution["actualParameters"]["rateCallbackSummary"]["invocationCount"], 0)

        matrix = storage.read_json(REAL_WORKER.parent.parent / "docs" / "stage9-evidence" / "gpu-capability-matrix.json")
        capability = next(row for row in matrix["rows"] if row["model"] == "multiParticle" and row["dimension"] == 2 and row["operation"] == "etch")
        direct = storage.read_json(REAL_WORKER.parent.parent / capability["evidence"]["directReference"])
        direct_step = direct["steps"][project["steps"][0]["id"]]
        callback_summary = execution["actualParameters"]["rateCallbackSummary"]
        direct_callback = direct_step["callback"]
        self.assertEqual(callback_summary["invocationCount"], direct_callback["count"])
        self.assertEqual(callback_summary["materials"], direct_callback["materials"])
        self.assertEqual(callback_summary["samples"], direct_callback["samples"])
        # The direct fixture stores each native callback's input vector, not
        # pre-aggregated sums. Derive the independent totals from those samples
        # and allow only last-bit floating-point accumulation differences.
        direct_samples = direct_callback["samples"]
        self.assertEqual(len(direct_samples), direct_callback["count"])
        species_count = len(project["steps"][0]["species"])
        direct_flux_sums = [sum(float(sample["fluxes"][index]) for sample in direct_samples)
                            for index in range(species_count)]
        direct_flux_maxima = [max(float(sample["fluxes"][index]) for sample in direct_samples)
                              for index in range(species_count)]
        direct_mean_fluxes = [value / direct_callback["count"] for value in direct_flux_sums]
        for actual, expected in zip(callback_summary["fluxSums"], direct_flux_sums):
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12))
        self.assertEqual(callback_summary["fluxMaxima"], direct_flux_maxima)
        for actual, expected in zip(callback_summary["meanFluxBySpecies"], direct_mean_fluxes):
            self.assertTrue(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12))
        snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{project['steps'][0]['id']}"))
        direct_snapshot = direct_step["snapshot"]
        self.assertEqual(snapshot["points"], direct_snapshot["points"])
        self.assertEqual(snapshot["lines"], direct_snapshot["cells"])
        self.assertEqual(snapshot["cellData"]["materialIds"], direct_snapshot["materialIds"])
        self.assertEqual(snapshot["materialIdsByLevelSetIndex"], direct_snapshot["materialIdsByLevelSetIndex"])

        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["executionRequirements"]["steps"][0]["engine"], "GPU_DISK")
        replay_submit = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replay_submit[0], 202, replay_submit[2].decode("utf-8", errors="replace"))
        replay_id = json_body(replay_submit)["runId"]
        replay_status = await self.wait_for_terminal(replay_id, timeout=90)
        self.assertEqual(replay_status["status"], "succeeded", replay_status.get("failureReason"))
        replay_manifest = storage.read_json(storage.run_path(replay_id) / "manifest.json")
        self.assertEqual(replay_manifest["actualExecution"][0]["actualEngine"], "GPU_DISK")
        replay_snapshot = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/{project['steps'][0]['id']}"))
        for key in ("points", "lines", "cellData", "materialIdsByLevelSetIndex"):
            self.assertEqual(snapshot[key], replay_snapshot[key], key)

    async def test_worker_exception_and_disk_preflight_are_reported(self) -> None:
        project = plane_project("异常和磁盘预检验收")
        project["steps"] = [{"id": "step_etch_002", "name": "刻蚀", "enabled": True, "operation": "etch",
                             "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1}]
        with patch("backend.storage.check_disk_preflight", side_effect=OSError("injected disk reserve failure")):
            disk_failure = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(disk_failure[0], 507)
        self.assertIn("injected disk reserve failure", json_body(disk_failure)["detail"])

        with patch("backend.supervisor.WORKER", REAL_WORKER.parent.parent / "tests" / "fixtures" / "fail_worker.py"):
            submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
            self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
            run_id = json_body(submitted)["runId"]
            status = await self.wait_for_terminal(run_id)
        self.assertEqual(status["status"], "failed")
        self.assertIn("injected worker exception", status["failureReason"])
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["childRecord"]["terminationReason"], "exited")
        self.assertNotEqual(manifest["childRecord"]["returnCode"], 0)
        self.assertEqual(manifest["childRecord"]["remainingOwnedGroupMembers"], [])

    async def test_normal_shutdown_cancels_and_reaps_active_worker(self) -> None:
        project = plane_project("正常关闭回收验收")
        project["steps"] = [{"id": "step_etch_003", "name": "刻蚀", "enabled": True, "operation": "etch",
                             "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1}]
        normalized = Project.model_validate(project).model_dump(mode="json", exclude_none=True)
        manager = Supervisor(os.dup(app.state.supervisor.lock_fd))
        with patch("backend.supervisor.WORKER", REAL_WORKER.parent.parent / "tests" / "fixtures" / "sleep_worker.py"):
            submitted = manager.start("simulate", normalized)
            run_id = submitted["runId"]
            deadline = time.monotonic() + 5
            while manager._status(run_id) != "running" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(manager._status(run_id), "running")
            manager.close()
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["status"], "cancelled")
        self.assertEqual(manifest["childRecord"]["terminationReason"], "supervisor_cancelled")
        self.assertEqual(manifest["childRecord"]["remainingOwnedGroupMembers"], [])

    async def test_multi_ion_gpu_flux_replay_is_rejected_before_a_worker_starts(self) -> None:
        """A direct supervisor replay must not queue a run the worker must refuse.

        ``per-species-native-mesh-merge-v1`` needs no patched-runtime
        fingerprint, so keying the policy off ``needs_native_patch`` used to
        leave this flux replay pending on ViennaPS 4.7.0 even though the GPU
        binding can only emit one species field per native flux call.
        """
        from importlib.metadata import version
        from backend.engine import engine_version, multi_ion_gpu_error
        from backend.models import ProjectV7
        from backend.supervisor import ReplayMismatch
        from tests.test_stage7 import multi_project

        policy = multi_ion_gpu_error()
        if policy is None:
            self.skipTest(f"ViennaPS {engine_version()} keeps multi-ion GPU support")

        project = multi_project(2, "etch")
        step = project["steps"][0]
        second = json.loads(json.dumps(step["species"][0]))
        second["label"] = "ion_second"
        step["species"].append(second)
        step["rateCoefficients"]["defaultBySpecies"]["ion_second"] = 0.02
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        normalized = ProjectV7.model_validate(project).model_dump(mode="json", exclude_none=True)
        self.assertEqual(sum(row["kind"] == "ion" for row in normalized["steps"][0]["species"]), 2)

        # The API validates the same project before queueing, so all three
        # layers must refuse with the same policy text.
        from backend.validation import ProjectInputError, validate_project
        with self.assertRaises(ProjectInputError) as validation:
            validate_project(normalized, kind="simulate")
        self.assertIn(policy, " ".join(issue.message for issue in validation.exception.issues))

        replay = {
            "analysisMode": "flux",
            "analysisStepId": step["id"],
            "seed": 271828,
            "viennapsVersion": version("viennaps"),
            "viennalsVersion": version("viennals"),
            "stepsById": {step["id"]: {
                "stepId": step["id"], "engine": "GPU_DISK", "seed": 271828,
                "fluxStrategy": "per-species-native-mesh-merge-v1"}},
        }
        manager = Supervisor(os.dup(app.state.supervisor.lock_fd))
        try:
            before = set(storage.RUNS.iterdir()) if storage.RUNS.is_dir() else set()
            with self.assertRaises(ReplayMismatch) as context:
                manager.start("flux", normalized, replay=replay)
            self.assertEqual(str(context.exception), policy)
            after = set(storage.RUNS.iterdir()) if storage.RUNS.is_dir() else set()
            self.assertEqual(before, after, "a refused replay must not create a run directory")
        finally:
            manager.close()

    async def test_random_cpu_particle_replay_matches_mesh(self) -> None:
        project = default_project()
        project["name"] = "CPU_DISK 重放验收"
        project["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
        project["domain"]["gridDelta"] = 0.2
        project["domain"]["xExtent"] = 4.0
        project["steps"] = [{"id": "step_particle_01", "name": "单粒子刻蚀", "enabled": True,
                             "operation": "etch", "model": "singleParticle", "rateMagnitude": 0.2,
                             "duration": 0.2, "stickingProbability": 0.5, "sourceExponent": 1.0}]
        project["solver"]["random"] = {"mode": "random"}
        project["solver"]["enginePreference"] = "auto"
        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        original_id = json_body(submitted)["runId"]
        original_status = await self.wait_for_terminal(original_id, timeout=90)
        self.assertEqual(original_status["status"], "succeeded", original_status.get("failureReason"))

        replay_response = await asgi_request(f"/api/runs/{original_id}/replay-project")
        self.assertEqual(replay_response[0], 200)
        replay = json_body(replay_response)
        requirement = replay["executionRequirements"]["steps"][0]
        self.assertEqual(requirement["engine"], "CPU_DISK")
        self.assertEqual(replay["normalizedProject"]["solver"]["random"]["mode"], "fixed")
        self.assertEqual(replay["normalizedProject"]["solver"]["random"]["seed"], requirement["seed"])

        unsupported = json.loads(json.dumps(replay))
        unsupported["executionRequirements"]["viennapsVersion"] = "0.0.0"
        rejected = await asgi_request("/api/runs/replay", method="POST", payload=unsupported)
        self.assertEqual(rejected[0], 422)
        detail = json_body(rejected)["detail"]
        self.assertIn("requires ViennaPS", detail["message"])
        self.assertIn("never reinterpreted", detail["message"])
        self.assertEqual(detail["requiredVersions"]["viennaps"], "0.0.0")

        replayed = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replayed[0], 202, replayed[2].decode("utf-8", errors="replace"))
        replay_id = json_body(replayed)["runId"]
        replay_status = await self.wait_for_terminal(replay_id, timeout=90)
        self.assertEqual(replay_status["status"], "succeeded", replay_status.get("failureReason"))

        first = json_body(await asgi_request(f"/api/runs/{original_id}/snapshots/step_particle_01"))
        second = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/step_particle_01"))
        self.assertEqual(set(first["cellData"]["materialIds"]), set(second["cellData"]["materialIds"]))
        self.assertLessEqual(mesh_distance(first, second), 2 * project["domain"]["gridDelta"] + 1e-9)

    async def test_schema7_replay_rejects_engine_preference_mismatch(self) -> None:
        from tests.test_stage7 import multi_project
        from backend.engine import engine_version, viennals_version

        for preference, engine in (("cpu", "GPU_DISK"), ("gpu", "CPU_DISK")):
            with self.subTest(preference=preference, engine=engine):
                project = multi_project(2, "etch")
                project["solver"]["enginePreference"] = preference
                replay = {
                    "format": "viennaps-replay", "schemaVersion": 7, "replayMode": "from-initial",
                    "analysisMode": "process", "normalizedProject": project,
                    "executionRequirements": {
                        "viennapsVersion": engine_version(),
                        "viennalsVersion": viennals_version(), "cpuThreads": 1,
                        "steps": [{"stepId": project["steps"][0]["id"], "engine": engine, "seed": 271828}],
                    },
                }
                response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
                self.assertEqual(response[0], 422, response[2].decode("utf-8", errors="replace"))
                self.assertIn("match normalizedProject.solver.enginePreference", json_body(response)["detail"]["message"])

    async def test_old_runtime_replay_is_rejected_not_reinterpreted(self) -> None:
        from tests.test_stage7 import multi_project
        from backend.engine import engine_version

        if engine_version() == "4.6.2":
            self.skipTest("running on the original 4.6.2 runtime; nothing to reject")
        project = multi_project(2, "etch")
        replay = {
            "format": "viennaps-replay", "schemaVersion": 7, "replayMode": "from-initial",
            "analysisMode": "process", "normalizedProject": project,
            "executionRequirements": {
                "viennapsVersion": "4.6.2", "viennalsVersion": "5.8.5", "cpuThreads": 1,
                "steps": [{"stepId": project["steps"][0]["id"], "engine": "CPU_DISK", "seed": 271828}],
            },
        }
        response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(response[0], 422, response[2].decode("utf-8", errors="replace"))
        detail = json_body(response)["detail"]
        self.assertEqual(detail["requiredVersions"], {"viennaps": "4.6.2", "viennals": "5.8.5"})
        self.assertIn("never reinterpreted", detail["message"])
        self.assertIn("4.6.2", detail["message"])
        self.assertIn(engine_version(), detail["message"])

    async def test_plugin_step_envelope_runs_through_the_api(self) -> None:
        project = default_project()
        project["schemaVersion"] = 7
        project["steps"] = [
            {
                "id": "plugin_step_01",
                "name": "plugin isotropic",
                "enabled": True,
                "model": "plugin",
                "pluginId": "isotropic",
                "params": {
                    "operation": "etch",
                    "rateMagnitude": 0.1,
                    "duration": 1.0,
                    "rateMode": "uniform",
                },
            }
        ]
        submitted = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project})
        self.assertEqual(submitted[0], 202, submitted[2].decode("utf-8", errors="replace"))
        run_id = json_body(submitted)["runId"]
        status = await self.wait_for_terminal(run_id)
        self.assertEqual(status["status"], "succeeded", status)
        self.assertTrue(status.get("snapshots"))

    async def test_schema7_gpu_project_validation_does_not_gate_multiion_species_signatures(self) -> None:
        from tests.test_stage7 import multi_project
        from backend.engine import engine_version

        base = multi_project(2, "etch")
        base["solver"]["enginePreference"] = "gpu"
        accepted = await asgi_request("/api/projects/validate", method="POST", payload=base)
        self.assertEqual(accepted[0], 200, accepted[2].decode("utf-8", errors="replace"))
        self.assertTrue(json_body(accepted)["valid"])

        for label, species in (("single-neutral", [base["steps"][0]["species"][1]]),
                               ("eight-ions", [base["steps"][0]["species"][0] for _ in range(8)])):
            with self.subTest(configuration=label):
                candidate = json.loads(json.dumps(base))
                step = candidate["steps"][0]
                step["species"] = json.loads(json.dumps(species))
                for index, particle in enumerate(step["species"]):
                    particle["label"] = f"{label}-{index}"
                labels = [particle["label"] for particle in step["species"]]
                step["rateCoefficients"] = {"defaultBySpecies": {name: 0.025 for name in labels},
                                             "byMaterial": {"SiO2": {name: 0.04 for name in labels}}}
                response = await asgi_request("/api/projects/validate", method="POST", payload=candidate)
                multi_ion = label == "eight-ions"
                if multi_ion and engine_version() != "4.6.2":
                    # Stock 4.7.0 GPU engines silently drop every ion after the
                    # first, so the request fails fast instead of running wrong.
                    self.assertEqual(response[0], 422, response[2].decode("utf-8", errors="replace"))
                    message = json.dumps(json_body(response), ensure_ascii=False)
                    self.assertIn("silently drop every ion", message)
                    cpu = json.loads(json.dumps(candidate))
                    cpu["solver"]["enginePreference"] = "cpu"
                    cpu_response = await asgi_request("/api/projects/validate", method="POST",
                                                      payload=cpu)
                    self.assertEqual(cpu_response[0], 200,
                                     cpu_response[2].decode("utf-8", errors="replace"))
                    self.assertTrue(json_body(cpu_response)["valid"])
                    continue
                self.assertEqual(response[0], 200, response[2].decode("utf-8", errors="replace"))
                validation = json_body(response)
                self.assertTrue(validation["valid"])
                self.assertFalse(any("only converts 1 ion particle type" in warning
                                     or "no verified engine combination" in warning
                                     for warning in validation["warnings"]))


if __name__ == "__main__":
    unittest.main()
