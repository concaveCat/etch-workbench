"""Stage 4 acceptance: native ViennaLS solver controls, flux and field output."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import subprocess
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from backend import storage
from backend.hashing import requested_input_hash
from backend.main import app, intermediate_output_get, run_download
from backend.validation import ProjectInputError, validate_project, select_engine
from tests.test_api_integration import asgi_request, json_body
from tests.test_directional import project as base_project


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "stage4-evidence"
ADVECTION = {
    "spatialScheme": "WENO_3RD_ORDER",
    "temporalScheme": "RUNGE_KUTTA_2ND_ORDER",
    "adaptiveTimeStepping": True,
    "adaptiveTimeStepSubdivisions": 8,
    "calculateIntermediateVelocities": True,
    "checkDissipation": True,
    "dissipationAlpha": 0.7,
    "ignoreVoids": False,
    "velocityOutput": True,
}
RAYS = {
    "diskRadius": 0.2,
    "ignoreFluxBoundaries": True,
    "maxBoundaryHits": 240,
    "maxReflections": 6,
    "minNodeDistanceFactor": 0.12,
    "normalizationType": "MAX",
    "smoothingNeighbors": 2,
}


def project_v7(dimension: int, model: str = "singleParticle") -> dict:
    value = base_project(dimension)
    value["schemaVersion"] = 7
    value["name"] = f"Stage 4 {dimension}D {model}"
    value["domain"]["gridDelta"] = 0.2 if dimension == 2 else 0.5
    value["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    value["solver"].update({"raysPerPoint": 64, "random": {"mode": "fixed", "seed": 20261026},
                            "enginePreference": "cpu", "advection": copy.deepcopy(ADVECTION),
                            "rayTracing": copy.deepcopy(RAYS)})
    value["steps"] = [{"id": "step_stage4_native", "name": "Stage 4 native process", "enabled": True,
                       "operation": "etch", "model": model, "rateMode": "uniform",
                       "rateMagnitude": 0.2, "duration": 0.1,
                       **({"stickingProbability": 0.5, "sourceExponent": 1.0} if model == "singleParticle" else {})}]
    return value


class Stage4Contract(unittest.TestCase):
    def test_v7_validation_hashes_only_applicable_native_controls_and_keeps_legacy(self):
        particle = project_v7(2)
        parsed, _ = validate_project(particle, kind="simulate")
        base_hash = requested_input_hash(parsed)
        changed_ray = copy.deepcopy(particle)
        changed_ray["solver"]["rayTracing"]["maxReflections"] = 5
        self.assertNotEqual(base_hash, requested_input_hash(validate_project(changed_ray)[0]))
        changed_advection = copy.deepcopy(particle)
        changed_advection["solver"]["advection"]["spatialScheme"] = "WENO_5TH_ORDER"
        self.assertNotEqual(base_hash, requested_input_hash(validate_project(changed_advection)[0]))

        isotropic = project_v7(2, "isotropic")
        ray_ignored = copy.deepcopy(isotropic)
        ray_ignored["solver"]["rayTracing"]["maxReflections"] = 17
        self.assertEqual(requested_input_hash(validate_project(isotropic)[0]),
                         requested_input_hash(validate_project(ray_ignored)[0]))
        for key, invalid in (("spatialScheme", "UNKNOWN_SCHEME"),
                             ("temporalScheme", "ADAMS_BASHFORTH"),
                             ("adaptiveTimeStepSubdivisions", 0),
                             ("dissipationAlpha", float("nan"))):
            malformed = copy.deepcopy(particle)
            malformed["solver"]["advection"][key] = invalid
            with self.assertRaises(ProjectInputError):
                validate_project(malformed)
        malformed = copy.deepcopy(particle)
        malformed["solver"]["rayTracing"]["smoothingNeighbors"] = -1
        with self.assertRaises(ProjectInputError):
            validate_project(malformed)


class Stage4Workflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait_run(self, run_id: str) -> dict:
        for _ in range(900):
            response = await asgi_request(f"/api/runs/{run_id}")
            self.assertEqual(response[0], 200, response[2])
            info = json_body(response)
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail(f"bounded ViennaPS worker did not finish: {run_id}")

    async def direct_reference(self, value: dict, mode: str, label: str, engine: str = "CPU_DISK") -> tuple[dict, Path]:
        folder = EVIDENCE / f"direct-{label}"
        folder.mkdir(parents=True, exist_ok=True)
        input_path = EVIDENCE / f"{label}-direct-input.json"
        output_path = folder / "reference.json"
        guard = EVIDENCE / f"{label}-direct.guard.json"
        log = EVIDENCE / f"{label}-direct.log"
        input_path.write_text(json.dumps({"project": value, "mode": mode, "engine": engine},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(guard), "--log-path", str(log),
                   "--timeout-seconds", "60", "--max-rss-mib", "2048", "--max-log-bytes", "2097152", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage4_native_reference.py"),
                   str(input_path), str(output_path)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log.read_text(encoding="utf-8", errors="replace"))
        record = json.loads(guard.read_text(encoding="utf-8"))
        self.assertFalse(record["timedOut"])
        self.assertFalse(record["rssLimited"])
        self.assertEqual(record["remainingOwnedGroupMembers"], [])
        return json.loads(output_path.read_text(encoding="utf-8")), folder

    async def download_zip(self, run_id: str) -> bytes:
        response = await run_download(run_id)
        return b"".join([chunk async for chunk in response.body_iterator])

    def compare_flux(self, actual: dict, direct: dict) -> None:
        expected = direct["flux"]
        self.assertEqual(actual["source"], "viennaps.Process.calculateFlux")
        self.assertEqual(actual["meshIdentity"], expected["meshIdentity"])
        self.assertEqual(actual["points"], expected["points"])
        self.assertEqual(actual["topology"], expected["topology"])
        self.assertEqual(actual["pointData"], expected["pointData"])
        self.assertEqual(actual["cellData"], expected["cellData"])
        self.assertEqual(actual["fields"], expected["fields"])
        self.assertTrue(actual["fields"])
        for field in actual["fields"]:
            self.assertIsNone(field["nativeUnit"])
            values = actual[field["association"]]["scalars" if field["components"] == 1 else "vectors"][field["name"]]
            self.assertEqual(len(values), field["tuples"])
        self.assertEqual(actual["execution"]["actualEngine"], direct["engine"])
        self.assertEqual(actual["execution"]["actualSeed"], direct["seed"])

    async def test_real_single_flux_2d_3d_result_export_history_replay(self):
        evidence = []
        for dimension in (2, 3):
            value = project_v7(dimension)
            parsed, _ = validate_project(value, kind="simulate")
            normalized = parsed.model_dump(mode="json", exclude_none=True)
            step = normalized["steps"][0]
            engine = select_engine(normalized, step)
            direct, folder = await self.direct_reference(value, "flux", f"stage4-flux-{dimension}d", engine)

            saved = await asgi_request("/api/projects", method="POST", payload=value)
            self.assertEqual(saved[0], 201, saved[2])
            project_id = json_body(saved)["id"]
            reopened_response = await asgi_request(f"/api/projects/{project_id}")
            self.assertEqual(reopened_response[0], 200)
            reopened = json_body(reopened_response)["project"]
            self.assertEqual(reopened["schemaVersion"], 7)
            self.assertEqual(reopened["solver"]["advection"], value["solver"]["advection"])
            self.assertEqual(reopened["solver"]["rayTracing"], value["solver"]["rayTracing"])
            self.assertEqual(requested_input_hash(validate_project(reopened)[0]), requested_input_hash(parsed))

            started = await asgi_request("/api/runs/flux", method="POST", payload={"project": reopened, "stepId": step["id"]})
            self.assertEqual(started[0], 202, started[2])
            run_id = json_body(started)["runId"]
            info = await self.wait_run(run_id)
            flux_response = await asgi_request(f"/api/runs/{run_id}/flux-result")
            self.assertEqual(flux_response[0], 200, flux_response[2])
            flux = json_body(flux_response)
            self.compare_flux(flux, direct)
            self.assertEqual(info["kind"], "flux")
            manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
            selected = manifest["actualExecution"][0]
            self.assertEqual(selected["actualEngine"], engine)
            self.assertEqual(selected["actualSeed"], 20261026)
            for key, expected in direct["rayReadback"].items():
                self.assertEqual(flux["execution"]["rays"][key], expected, key)
            self.assertEqual(selected["actualParameters"]["rays"], flux["execution"]["rays"])
            self.assertIsNone(selected.get("duration"), "single flux must not invent a process duration")

            download = await self.download_zip(run_id)
            with zipfile.ZipFile(BytesIO(download)) as archive:
                names = set(archive.namelist())
                self.assertTrue({"project.json", "replay-project.json", "manifest.json", "flux.json", "flux.vtp"}.issubset(names))
                self.assertEqual(json.loads(archive.read("flux.json")), flux)
                replay = json.loads(archive.read("replay-project.json"))
                self.assertEqual(replay["analysisMode"], "flux")
                self.assertEqual(replay["analysisStepId"], step["id"])
                self.assertEqual(hashlib.sha256(archive.read("flux.vtp")).hexdigest(), direct["nativeVtp"]["sha256"])
            history = json_body(await asgi_request("/api/runs"))["runs"]
            self.assertTrue(any(row["runId"] == run_id and row["kind"] == "flux" for row in history))

            replay_response = await asgi_request("/api/runs/replay", method="POST", payload=replay)
            self.assertEqual(replay_response[0], 202, replay_response[2])
            replay_run_id = json_body(replay_response)["runId"]
            await self.wait_run(replay_run_id)
            replay_flux = json_body(await asgi_request(f"/api/runs/{replay_run_id}/flux-result"))
            self.compare_flux(replay_flux, direct)
            self.assertEqual(replay_flux["meshIdentity"], flux["meshIdentity"])
            evidence.append({"dimension": dimension, "runId": run_id, "replayRunId": replay_run_id,
                             "engine": engine, "seed": 20261026, "meshIdentity": flux["meshIdentity"],
                             "fieldNames": flux["fields"], "nativeVtpSha256": direct["nativeVtp"]["sha256"],
                             "exactFluxArraysMatchDirectViennaPS": True})
        (EVIDENCE / "flux-acceptance.json").write_text(json.dumps({"dimensions": evidence, "passed": True}, indent=2) + "\n", encoding="utf-8")

    async def test_advanced_advection_and_intermediate_native_fields(self):
        value = project_v7(2, "isotropic")
        direct, direct_dir = await self.direct_reference(value, "process", "stage4-advection-2d")
        started = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": value})
        self.assertEqual(started[0], 202, started[2])
        run_id = json_body(started)["runId"]
        info = await self.wait_run(run_id)
        row = info["actualExecution"][0]
        for key, expected in direct["advectionReadback"].items():
            self.assertEqual(row["actualParameters"][key], expected, key)
        actual = await asgi_request(f"/api/runs/{run_id}/snapshots/{value['steps'][0]['id']}")
        self.assertEqual(actual[0], 200, actual[2])
        snapshot = json_body(actual)
        reference = direct["surface"]
        self.assertEqual(snapshot["lines"], reference["topology"]["lines"])
        self.assertEqual(snapshot["cellData"]["materialIds"], reference["cellData"]["scalars"]["MaterialIds"])
        self.assertEqual(snapshot["points"], reference["points"])

        outputs = info["intermediateOutputs"]
        self.assertTrue(outputs, "velocityOutput must produce a retained ViennaLS VTP artifact")
        output = next((item for item in outputs if item["format"] == "VTP"), None)
        self.assertIsNotNone(output)
        names = {item["name"] for item in output["fields"]}
        self.assertTrue({"AdvectionVelocities", "Dissipation", "LSValues", "SegmentID"}.issubset(names))
        self.assertEqual(output["cellCounts"]["Verts"], output["pointCount"])
        self.assertTrue(all(item["nativeUnit"] is None for item in output["fields"]))
        direct_file = direct_dir / "ls_velocities_0.vtp"
        self.assertTrue(direct_file.is_file())
        self.assertEqual(output["sha256"], hashlib.sha256(direct_file.read_bytes()).hexdigest())
        file_name = output["fileName"].split("/")[-1]
        file_response = await intermediate_output_get(run_id, output["stepId"], file_name)
        content = Path(file_response.path).read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), output["sha256"])
        archive_response = await self.download_zip(run_id)
        with zipfile.ZipFile(BytesIO(archive_response)) as archive:
            self.assertIn(output["fileName"], archive.namelist())
            self.assertEqual(hashlib.sha256(archive.read(output["fileName"])).hexdigest(), output["sha256"])
        (EVIDENCE / "advection-acceptance.json").write_text(json.dumps({
            "runId": run_id, "requestedSolver": value["solver"]["advection"],
            "nativeReadback": row["actualParameters"], "intermediateOutput": output,
            "directReferenceVtpSha256": hashlib.sha256(direct_file.read_bytes()).hexdigest(),
            "exactGeometryAndIntermediateVtpMatchDirectViennaPS": True,
        }, indent=2) + "\n", encoding="utf-8")
