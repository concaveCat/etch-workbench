"""Stage 3 acceptance for native .vpsd save, migration, replay and continuation."""
from __future__ import annotations

import asyncio
import copy
import io
import json
import secrets
import unittest
import zipfile
from pathlib import Path
from starlette.requests import Request
from fastapi import HTTPException

from backend.engine import engine_version, viennals_version
from backend import storage
from backend.hashing import requested_input_hash
from backend.main import app, continuation_package_export, continuation_package_import
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "stage3-evidence"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


def project(dimension: int) -> dict:
    value = {
        "schemaVersion": 5, "name": f"原生状态续算 {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            {"id": "step_first_etch", "name": "保存点前刻蚀", "enabled": True, "operation": "etch", "model": "isotropic",
             "rateMagnitude": 0.2, "duration": 0.4, "rateMode": "uniform"},
            {"id": "step_continue_w", "name": "续算沉积钨", "enabled": True, "operation": "deposit", "model": "isotropic",
             "rateMagnitude": 0.2, "duration": 0.4, "depositMaterial": "W", "rateMode": "uniform"},
        ],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu", "timeoutSeconds": 60},
    }
    if dimension == 2:
        value["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        value["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface", "visibleMaterialIds": [],
                           "opacityByMaterial": {}, "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                           "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return value


def signature(snapshot: dict) -> dict:
    return {"points": snapshot["points"], "cells": snapshot.get("lines", snapshot.get("triangles")),
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialMap": snapshot["materialIdsByLevelSetIndex"], "gridDelta": snapshot["gridDelta"]}


class DomainContinuationValidation(unittest.TestCase):
    def test_schema6_state_contract_and_old_schema_preservation(self):
        for dimension in (2, 3):
            old = project(dimension)
            parsed_old, _ = validate_project(old, kind="simulate")
            self.assertEqual(parsed_old.schemaVersion, 5)
            state = {"formatVersion": 1, "stateId": "a" * 64, "structureHash": "b" * 64,
                     "dimension": dimension, "gridDelta": 0.2, "xExtent": 4.0,
                     **({"yExtent": 4.0} if dimension == 3 else {}), "boundary": "reflective",
                     "boundaryConditions": ["reflective", "infinite"] if dimension == 2 else ["reflective", "reflective", "infinite"],
                     "materialIdsByLevelSetIndex": [10], "levelSetCount": 1,
                     "viennapsVersion": "4.6.2", "viennalsVersion": "5.8.5"}
            promoted = {**old, "schemaVersion": 6, "steps": [], "continuationState": state}
            # A structurally valid hash is produced by a real project object, not a guessed string.
            from backend.models import normalized_geometry_v3, project_structure_hash
            parsed = validate_project(old)[0]
            state["structureHash"] = project_structure_hash(dimension, parsed.units, parsed.domain, normalized_geometry_v3(parsed))
            state["materialIdsByLevelSetIndex"] = [10]
            valid = validate_project(promoted)[0]
            base_hash = requested_input_hash(valid)
            provenance_only = copy.deepcopy(promoted)
            provenance_only["continuationState"].update({"sourceRunId": "1" * 24, "sourceStepId": "step_first_etch"})
            self.assertEqual(base_hash, requested_input_hash(validate_project(provenance_only)[0]))
            changed = copy.deepcopy(promoted); changed["domain"]["gridDelta"] = 0.25
            with self.assertRaises(ProjectInputError):
                validate_project(changed)
            changed = copy.deepcopy(promoted); changed["continuationState"]["boundaryConditions"][-1] = "reflective"
            with self.assertRaises(ProjectInputError):
                validate_project(changed)


class DomainContinuationWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.original_storage = (storage.VAR, storage.PROJECTS, storage.RUNS, storage.DOMAIN_STATES, storage.LOCK_PATH)
        isolated = EVIDENCE / f"workbench-data-{secrets.token_hex(4)}"
        self.isolated_storage = isolated
        storage.VAR = isolated
        storage.PROJECTS = isolated / "projects"
        storage.RUNS = isolated / "runs"
        storage.DOMAIN_STATES = isolated / "domain-states"
        storage.LOCK_PATH = isolated / "workbench.lock"
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)
        storage.VAR, storage.PROJECTS, storage.RUNS, storage.DOMAIN_STATES, storage.LOCK_PATH = self.original_storage

    async def wait(self, run_id):
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("native continuation worker exceeded acceptance timeout")

    async def direct_reference(self, value, label):
        source = EVIDENCE / f"{label}-input.json"
        output = EVIDENCE / f"{label}-direct-reference.json"
        state = EVIDENCE / f"{label}-direct-reference.vpsd"
        guard = EVIDENCE / f"{label}-reference-guard.json"
        log = EVIDENCE / f"{label}-reference.log"
        source.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                   "--record", str(guard), "--log-path", str(log), "--timeout-seconds", "60", "--max-rss-mib", "2048",
                   "--max-log-bytes", "2097152", "--", str(ROOT / ".venv/bin/python"),
                   str(ROOT / "tests/fixtures/domain_continuation_direct_reference.py"), str(source), str(output), str(state)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log.read_text(encoding="utf-8", errors="replace"))
        record = json.loads(guard.read_text(encoding="utf-8"))
        self.assertEqual(record["terminationReason"], "exited")
        self.assertEqual(record["remainingOwnedGroupMembers"], [])
        self.assertTrue(state.is_file())
        return json.loads(output.read_text(encoding="utf-8"))

    async def run_native(self, project_value):
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": project_value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    async def export_package(self, payload):
        # Invoke the route and consume its real streaming ZIP body. The shared
        # API test receive stub intentionally never emits http.disconnect, so a
        # POST StreamingResponse would keep its disconnect watcher open.
        response = await continuation_package_export(payload)
        chunks = [chunk async for chunk in response.body_iterator]
        if response.background is not None:
            response.background.func(*response.background.args, **response.background.kwargs)
        return 200, {}, b"".join(chunks)

    async def import_package(self, package):
        delivered = False
        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": package, "more_body": False}
            return {"type": "http.disconnect"}
        request = Request({"type": "http", "method": "POST", "scheme": "http", "path": "/api/continuation-package/import",
                           "headers": [(b"content-type", b"application/zip"), (b"content-length", str(len(package)).encode())]}, receive)
        try:
            return 200, await continuation_package_import(request)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}

    async def test_real_2d_3d_state_roundtrip_continuation_and_portable_packages(self):
        evidence = []
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                source_project = project(dimension)
                print(f"stage3 {dimension}D: running bounded independent Writer/Reader reference", flush=True)
                reference = await self.direct_reference(source_project, f"continuation-{dimension}d")

                first_project = copy.deepcopy(source_project)
                first_project["steps"] = [first_project["steps"][0]]
                source_run, source_info = await self.run_native(first_project)
                print(f"stage3 {dimension}D: original run completed {source_run}", flush=True)
                self.assertEqual(source_info["runMode"], "from-initial")
                saved_row = next(row for row in source_info["snapshots"] if row["stepId"] == "step_first_etch")
                native_ref = saved_row["nativeState"]
                self.assertEqual(native_ref["viennapsVersion"], engine_version())
                self.assertEqual(native_ref["viennalsVersion"], viennals_version())
                self.assertEqual(native_ref["boundaryConditions"], ["reflective", "infinite"] if dimension == 2 else ["reflective", "reflective", "infinite"])
                self.assertEqual(native_ref["materialIdsByLevelSetIndex"], reference["afterRead"]["materialMap"])
                self.assertEqual(native_ref["levelSetCount"], len(reference["afterRead"]["materialMap"]))
                state_path = storage.run_path(source_run) / native_ref["fileName"]
                self.assertEqual(storage.file_sha256(state_path), native_ref["stateId"])
                self.assertGreater(state_path.stat().st_size, 0)

                result = json_body(await asgi_request(f"/api/runs/{source_run}/continuation-project/step_first_etch"))
                print(f"stage3 {dimension}D: continuation project prepared", flush=True)
                continuation = result["project"]
                self.assertEqual(continuation["schemaVersion"], 6)
                self.assertEqual(continuation["steps"], [])
                continued_project = copy.deepcopy(continuation)
                continued_project["steps"] = [source_project["steps"][1]]
                validated, _ = validate_project(continued_project, kind="simulate")
                continued_project = validated.model_dump(mode="json", exclude_none=True)

                # Save, reopen, export and import the state package without absolute paths.
                saved = await asgi_request("/api/projects", method="POST", payload=continuation)
                self.assertEqual(saved[0], 201, saved[2])
                project_id = json_body(saved)["id"]
                reopened = json_body(await asgi_request(f"/api/projects/{project_id}"))["project"]
                self.assertEqual(requested_input_hash(validate_project(reopened)[0]), requested_input_hash(validate_project(continuation)[0]))
                package_response = await self.export_package({"project": reopened})
                print(f"stage3 {dimension}D: portable package exported", flush=True)
                self.assertEqual(package_response[0], 200, package_response[2])
                package_bytes = package_response[2]
                with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
                    self.assertEqual(set(archive.namelist()), {"project.json", "domain-state.vpsd"})
                    self.assertNotIn(str(ROOT), archive.read("project.json").decode("utf-8"))
                imported_status, imported_body = await self.import_package(package_bytes)
                print(f"stage3 {dimension}D: portable package imported", flush=True)
                self.assertEqual(imported_status, 200, imported_body)
                imported = imported_body["project"]
                self.assertEqual(requested_input_hash(validate_project(imported)[0]), requested_input_hash(validate_project(reopened)[0]))

                continuation_run, continued_info = await self.run_native(continued_project)
                print(f"stage3 {dimension}D: continuation run completed {continuation_run}", flush=True)
                self.assertEqual(continued_info["runMode"], "from-domain-state")
                self.assertEqual(continued_info["initialState"]["stateId"], native_ref["stateId"])
                initial_row = continued_info["snapshots"][0]
                initial_snapshot = json_body(await asgi_request(f"/api/runs/{continuation_run}/snapshots/initial"))
                actual_initial = signature(initial_snapshot)
                expected_initial = {"points": reference["afterRead"]["points"], "cells": reference["afterRead"]["cells"],
                                    "materialIds": reference["afterRead"]["materialIds"], "materialMap": reference["afterRead"]["materialMap"],
                                    "gridDelta": reference["afterRead"]["gridDelta"]}
                self.assertEqual(actual_initial, expected_initial)
                self.assertTrue(initial_row["nativeState"]["fileName"].endswith(".vpsd"))
                final_row = continued_info["snapshots"][-1]
                final_snapshot = json_body(await asgi_request(f"/api/runs/{continuation_run}/snapshots/{final_row['stepId']}"))
                actual_final = signature(final_snapshot)
                expected_final = {"points": reference["resumed"]["points"], "cells": reference["resumed"]["cells"],
                                   "materialIds": reference["resumed"]["materialIds"], "materialMap": reference["resumed"]["materialMap"],
                                   "gridDelta": reference["resumed"]["gridDelta"]}
                self.assertEqual(actual_final, expected_final)

                replay = json_body(await asgi_request(f"/api/runs/{continuation_run}/replay-project"))
                self.assertEqual(replay["schemaVersion"], 6)
                self.assertEqual(replay["replayMode"], "from-domain-state")
                replay_package = await self.export_package({"project": replay["normalizedProject"], "replay": replay})
                self.assertEqual(replay_package[0], 200, replay_package[2])
                replay_zip_status, replay_zip = await self.import_package(replay_package[2])
                print(f"stage3 {dimension}D: replay package imported", flush=True)
                self.assertEqual(replay_zip_status, 200, replay_zip)
                imported_replay = replay_zip["replay"]
                replay_start = await asgi_request("/api/runs/replay", method="POST", payload=imported_replay)
                self.assertEqual(replay_start[0], 202, replay_start[2])
                replay_id = json_body(replay_start)["runId"]
                replay_info = await self.wait(replay_id)
                replay_final = json_body(await asgi_request(f"/api/runs/{replay_id}/snapshots/{final_row['stepId']}"))
                self.assertEqual(signature(replay_final), expected_final)
                self.assertEqual(replay_info["runMode"], "from-domain-state")

                run_zip = await asgi_request(f"/api/runs/{continuation_run}/download")
                self.assertEqual(run_zip[0], 200)
                with zipfile.ZipFile(io.BytesIO(run_zip[2])) as archive:
                    names = set(archive.namelist())
                    self.assertIn("domain-state.vpsd", names)
                    self.assertIn(initial_row["nativeState"]["fileName"], names)
                    self.assertIn(final_row["nativeState"]["fileName"], names)

                invalid_package = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(package_bytes)) as source, zipfile.ZipFile(invalid_package, "w", zipfile.ZIP_DEFLATED) as target:
                    bad_project = json.loads(source.read("project.json"))
                    bad_project["continuationState"]["stateId"] = "0" * 64
                    target.writestr("project.json", json.dumps(bad_project))
                    target.writestr("domain-state.vpsd", source.read("domain-state.vpsd"))
                rejected_status, _rejected = await self.import_package(invalid_package.getvalue())
                self.assertEqual(rejected_status, 422)

                row = {"dimension": dimension, "sourceRunId": source_run, "continuationRunId": continuation_run,
                       "replayRunId": replay_id, "nativeStateBytes": state_path.stat().st_size,
                       "stateRoundtripDirectMatch": True, "continuedMeshDirectMatch": True,
                       "replayMeshDirectMatch": True, "portablePackageImported": True}
                evidence.append(row)
                (EVIDENCE / f"continuation-{dimension}d-acceptance.json").write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
        (EVIDENCE / "continuation-acceptance.json").write_text(json.dumps({"dimensions": evidence, "passed": True}, indent=2) + "\n", encoding="utf-8")

