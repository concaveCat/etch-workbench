"""Stage 2 acceptance: recipe geometry operations run in the real Domain."""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import unittest
import zipfile
import io
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "stage2-evidence"


def project(dimension: int) -> dict:
    result = {
        "schemaVersion": 5, "name": f"配方几何操作 {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            {"id": "step_deposit_w", "name": "沉积钨", "enabled": True, "operation": "deposit", "model": "isotropic",
             "rateMagnitude": 0.2, "duration": 0.4, "depositMaterial": "W", "rateMode": "uniform"},
            {"id": "step_planarize", "name": "平坦化", "enabled": True, "model": "planarize", "cutoffHeight": 0.05},
            {"id": "step_union_box", "name": "增加盒体", "enabled": True, "model": "boolean", "booleanOperation": "union",
             "position": [0.0, 0.1, 0.0], "width": 1.2, "height": 0.6, "angle": 0.0, "applyToAll": True,
             **({"depth": 1.2} if dimension == 3 else {})},
            {"id": "step_intersection_box", "name": "裁切交集", "enabled": True, "model": "boolean", "booleanOperation": "intersection",
             "position": [0.0, -0.1, 0.0], "width": 1.6, "height": 0.5, "angle": 0.0, "applyToAll": True,
             **({"depth": 1.6} if dimension == 3 else {})},
            {"id": "step_difference_box", "name": "切除盒体", "enabled": True, "model": "boolean", "booleanOperation": "difference",
             "position": [0.0, 0.2, 0.0], "width": 0.4, "height": 0.4, "angle": 0.0, "applyToAll": True,
             **({"depth": 0.8} if dimension == 3 else {})},
            {"id": "step_remove_w", "name": "移除钨", "enabled": True, "model": "removeMaterial", "material": "W"},
            {"id": "step_disabled_flat", "name": "禁用平坦化", "enabled": False, "model": "planarize", "cutoffHeight": 0.3},
        ],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu", "timeoutSeconds": 60},
    }
    if dimension == 2:
        result["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        result["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface", "visibleMaterialIds": [],
                            "opacityByMaterial": {}, "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                            "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return result


class GeometryValidation(unittest.TestCase):
    def test_native_operation_contract_and_geometry_is_not_a_process(self):
        for dimension in (2, 3):
            value = project(dimension)
            parsed, _ = validate_project(value, kind="simulate")
            self.assertEqual(parsed.schemaVersion, 5)
            for step in parsed.steps[1:]:
                if step.enabled:
                    self.assertFalse(hasattr(step, "duration"))
                    self.assertFalse(hasattr(step, "rateMagnitude"))
            invalid = json.loads(json.dumps(value))
            invalid["steps"][1]["duration"] = 0.2
            with self.assertRaises(ProjectInputError):
                validate_project(invalid)
            invalid = json.loads(json.dumps(value))
            invalid["steps"][2]["booleanOperation"] = "invert"
            with self.assertRaises(ProjectInputError):
                validate_project(invalid)
            if dimension == 2:
                invalid = json.loads(json.dumps(value))
                invalid["steps"][2]["position"][2] = 0.1
                with self.assertRaises(ProjectInputError):
                    validate_project(invalid)
            else:
                invalid = json.loads(json.dumps(value))
                del invalid["steps"][2]["depth"]
                with self.assertRaises(ProjectInputError):
                    validate_project(invalid)


class GeometryWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id):
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded acceptance timeout")

    async def direct_reference(self, value, label):
        source = EVIDENCE / f"{label}-input.json"
        output = EVIDENCE / f"{label}-direct-reference.json"
        guard = EVIDENCE / f"{label}-reference-guard.json"
        log = EVIDENCE / f"{label}-reference.log"
        source.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                   "--record", str(guard), "--log-path", str(log), "--timeout-seconds", "60", "--max-rss-mib", "2048",
                   "--max-log-bytes", "2097152", "--", str(ROOT / ".venv/bin/python"),
                   str(ROOT / "tests/fixtures/geometry_operations_direct_reference.py"), str(source), str(output)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log.read_text(encoding="utf-8", errors="replace"))
        guard_record = json.loads(guard.read_text())
        self.assertEqual(guard_record["terminationReason"], "exited")
        self.assertEqual(guard_record["remainingOwnedGroupMembers"], [])
        return json.loads(output.read_text(encoding="utf-8"))

    async def run_and_compare(self, value, label):
        reference = await self.direct_reference(value, label)
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        info = await self.wait(run_id)
        self.assertEqual(info["stepTotal"], 6)
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["schemaVersion"], 5)
        self.assertEqual([item["stepId"] for item in info["snapshots"]], ["initial"] + [s["id"] for s in value["steps"] if s["enabled"]])
        self.assertNotIn("step_disabled_flat", [item["stepId"] for item in info["snapshots"]])
        topology_key = "lines" if value["dimension"] == 2 else "triangles"
        errors = []
        for row in info["snapshots"]:
            snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])
            actual = storage.read_json(storage.run_path(run_id) / row["snapshot"])
            expected = reference[row["stepId"]]
            self.assertEqual(actual[topology_key], expected["cells"], row["stepId"])
            self.assertEqual(actual["cellData"]["materialIds"], expected["materialIds"], row["stepId"])
            self.assertEqual(actual["materialIdsByLevelSetIndex"], expected["materialMap"], row["stepId"])
            self.assertEqual(len(actual["points"]), len(expected["points"]))
            error = max((math.dist(a, b) for a, b in zip(actual["points"], expected["points"])), default=0.0)
            self.assertLessEqual(error, 1e-10, row["stepId"])
            errors.append(error)

        actual_steps = {item["stepId"]: item for item in info["actualExecution"]}
        for step_id, model in [("step_planarize", "planarize"), ("step_union_box", "boolean"),
                               ("step_intersection_box", "boolean"), ("step_difference_box", "boolean"),
                               ("step_remove_w", "removeMaterial")]:
            record = actual_steps[step_id]
            self.assertEqual(record["model"], model)
            self.assertEqual(record["actualEngine"], "not-applicable")
            self.assertIsNone(record.get("actualRate"))
            self.assertNotIn("duration", record)
            self.assertFalse(record["noOp"])
        self.assertEqual(actual_steps["step_union_box"]["actualParameters"]["nativeBooleanEnum"], "UNION")
        self.assertEqual(actual_steps["step_intersection_box"]["actualParameters"]["nativeBooleanEnum"], "INTERSECT")
        self.assertEqual(actual_steps["step_difference_box"]["actualParameters"]["nativeBooleanEnum"], "RELATIVE_COMPLEMENT")

        project_response = await asgi_request("/api/projects", method="POST", payload=value)
        self.assertEqual(project_response[0], 201, project_response[2])
        project_id = json_body(project_response)["id"]
        reopened = json_body(await asgi_request(f"/api/projects/{project_id}"))["project"]
        normalized = validate_project(value)[0].model_dump(mode="json", exclude_none=True)
        self.assertEqual(reopened, normalized)
        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["schemaVersion"], 5)
        self.assertEqual(replay["executionRequirements"]["steps"], [])
        zip_response = await asgi_request(f"/api/runs/{run_id}/download")
        self.assertEqual(zip_response[0], 200)
        with zipfile.ZipFile(io.BytesIO(zip_response[2])) as archive:
            self.assertIn("replay-project.json", archive.namelist())
            for step_id in actual_steps:
                self.assertIn(f"{step_id}.json", archive.namelist())
        replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(replay_response[0], 202, replay_response[2])
        replay_id = json_body(replay_response)["runId"]
        replay_info = await self.wait(replay_id)
        for row in info["snapshots"]:
            snapshot_store.ensure_materialized(storage.run_path(run_id), row["stepId"])
            snapshot_store.ensure_materialized(storage.run_path(replay_id), row["stepId"])
            original = storage.read_json(storage.run_path(run_id) / row["snapshot"])
            repeated = storage.read_json(storage.run_path(replay_id) / f"{row['stepId']}.json")
            self.assertEqual(original[topology_key], repeated[topology_key], row["stepId"])
            self.assertEqual(original["cellData"]["materialIds"], repeated["cellData"]["materialIds"], row["stepId"])
            self.assertEqual(original["materialIdsByLevelSetIndex"], repeated["materialIdsByLevelSetIndex"], row["stepId"])
            self.assertEqual(original["points"], repeated["points"], row["stepId"])
        history = json_body(await asgi_request(f"/api/runs/{run_id}"))
        self.assertEqual(history["actualExecution"], info["actualExecution"])
        self.assertEqual(history["actualExecution"][-1]["actualParameters"]["material"], "W")
        result = {"runId": run_id, "replayRunId": replay_id, "dimension": value["dimension"],
                  "versions": manifest["versions"], "nativeReferenceTopologyMaterialsAndCoordinatesMatch": True,
                  "maxCoordinateErrors": errors, "coordinateTolerance": 1e-10,
                  "replayMatches": True, "historyMatches": True, "disabledStepExcluded": True}
        (EVIDENCE / f"{label}-comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result

    async def test_real_2d_and_3d_operations_reference_storage_and_replay(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                await self.run_and_compare(project(dimension), f"geometry-operations-{dimension}d")
