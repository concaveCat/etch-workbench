"""DirectionalProcess schema, native, persistence and replay acceptance."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import subprocess
import unittest
import zipfile
import io
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.hashing import requested_input_hash
from backend.main import app
from backend.validation import ProjectInputError, select_engine, validate_project
from tests.test_api_integration import asgi_request, json_body


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "directional-evidence"


def project(dimension: int = 2, geometry: str = "plane") -> dict:
    value = {
        "schemaVersion": 5,
        "name": f"定向过程 {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159}, "enginePreference": "cpu", "timeoutSeconds": 30},
    }
    if geometry == "trench":
        value["geometry"] = {"kind": "trench", "width": 1.6, "depth": 1.2, "taperAngle": 0.0,
                              "maskHeight": 0.6, "maskTaperAngle": 0.0, "substrateMaterial": "Si", "maskMaterial": "Mask"}
    if dimension == 2:
        value["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        value["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface",
                           "visibleMaterialIds": [], "opacityByMaterial": {},
                           "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                           "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return value


def directional_step(*, operation="etch", direction=None, directional=0.3, isotropic=0.1,
                     visibility=True, deposit_material=None, step_id="step_directional"):
    step = {"id": step_id, "name": "定向工艺", "enabled": True, "operation": operation,
            "model": "directional", "direction": direction or [0.0, -1.0, 0.0],
            "directionalRateMagnitude": directional, "isotropicRateMagnitude": isotropic,
            "calculateVisibility": visibility, "duration": 0.4}
    if operation == "deposit":
        step["depositMaterial"] = deposit_material or "W"
    return step


def rate_set_step(*, operation="etch", rate_sets=None, deposit_material=None,
                  step_id="step_rate_sets", duration=0.3, **extra):
    step = {"id": step_id, "name": "多集合定向", "enabled": True, "operation": operation,
            "model": "directional", "duration": duration,
            "rateSets": rate_sets if rate_sets is not None else [
                {"direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.15,
                 "isotropicVelocity": 0.05},
                {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.1},
            ]}
    if operation == "deposit":
        step["depositMaterial"] = deposit_material or "W"
    step.update(extra)
    return step


class DirectionalContract(unittest.TestCase):
    def test_validation_hash_mapping_and_no_ray_fields(self):
        for dimension in (2, 3):
            p = project(dimension)
            p["steps"] = [directional_step(direction=[0.0, -1.0, 0.0] if dimension == 2 else [0.0, 0.0, -1.0])]
            parsed, _ = validate_project(p, kind="simulate")
            baseline = requested_input_hash(parsed)
            scaled = copy.deepcopy(p)
            if dimension == 2:
                scaled["steps"][0]["direction"] = [0.0, -0.5, 0.0]
            else:
                scaled["steps"][0]["direction"] = [0.0, 0.0, -0.5]
            self.assertEqual(baseline, requested_input_hash(validate_project(scaled)[0]))
            changed = copy.deepcopy(p)
            changed["steps"][0]["directionalRateMagnitude"] = 0.2
            self.assertNotEqual(baseline, requested_input_hash(validate_project(changed)[0]))
            bad_cases = []
            bad = copy.deepcopy(p); bad["steps"][0]["direction"] = [0.0, 0.0, 0.0]; bad_cases.append(bad)
            if dimension == 2:
                bad = copy.deepcopy(p); bad["steps"][0]["direction"][2] = 1.0; bad_cases.append(bad)
            bad = copy.deepcopy(p); bad["steps"][0]["rayTracingParameters"] = {"raysPerPoint": 64}; bad_cases.append(bad)
            bad = copy.deepcopy(p); bad["steps"][0]["direction"] = [0.0, 1.01, 0.0]; bad_cases.append(bad)
            for bad in bad_cases:
                with self.assertRaises(ProjectInputError):
                    validate_project(bad)
            gpu_preferred = copy.deepcopy(p); gpu_preferred["solver"]["enginePreference"] = "gpu"
            parsed_gpu, _ = validate_project(gpu_preferred, kind="simulate")
            normalized_gpu = parsed_gpu.model_dump(mode="json", exclude_none=True)
            self.assertEqual(select_engine(normalized_gpu, normalized_gpu["steps"][0]), "not-applicable")

    def test_scalar_request_hashes_are_frozen(self):
        """Omitting ``rateSets`` must not move any existing request hash."""
        frozen = {
            (2, "etch"): "649460277660d2d0d2553f3d3c58432b39e14f084e9be0a16403adb891dcfecb",
            (2, "zero"): "1f24006652c81b4484cc71d3edc6a5b16779cadeb4fe0cacbd81c5e1112f85f6",
            (3, "etch"): "be995601e436454b8ac06d6fefb3abbf35f6b98f73627d1c9a934181a05393ae",
            (3, "zero"): "e56b597f81b0214ddb5b44b26f769d81bc8695c531a57c38e93d9c55cad48ad9",
        }
        for dimension in (2, 3):
            direction = [0.0, -1.0, 0.0] if dimension == 2 else [0.0, 0.0, -1.0]
            for label, step in (
                ("etch", directional_step(direction=direction, directional=0.3, isotropic=0.1)),
                ("zero", directional_step(directional=0.0, isotropic=0.0,
                                          step_id="step_directional_zero")),
            ):
                p = project(dimension)
                p["steps"] = [step]
                self.assertEqual(requested_input_hash(validate_project(p)[0]),
                                 frozen[(dimension, label)])

    def test_rate_set_mask_materials_are_tracked_material_references(self):
        from backend.materials import referenced_material_names
        names = referenced_material_names({"geometry": {"kind": "plane"}, "steps": [
            {"model": "directional", "rateSets": [
                {"direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.1,
                 "maskMaterials": ["CustomHardmask", "Mask"]}]}]})
        # A custom material named only in a per-set mask must stay a real
        # reference, otherwise project materialDefinitions drop it on reopen.
        self.assertEqual(names, {"CustomHardmask", "Mask"})

    def test_rate_set_validation_hash_normalization_and_mutual_exclusion(self):
        p = project(2)
        p["steps"] = [rate_set_step()]
        parsed, warnings = validate_project(p)
        self.assertEqual(warnings, [])
        baseline = requested_input_hash(parsed)
        single_set = copy.deepcopy(p)
        single_set["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.3, "isotropicVelocity": 0.05}])]
        scalar_equivalent = copy.deepcopy(p)
        scalar_equivalent["steps"] = [directional_step(directional=0.3, isotropic=0.05)]
        # Physically equal inputs in different schema shapes must hash differently.
        self.assertNotEqual(requested_input_hash(validate_project(single_set)[0]),
                            requested_input_hash(validate_project(scalar_equivalent)[0]))
        self.assertNotEqual(baseline, requested_input_hash(validate_project(single_set)[0]))

        # Directions are normalized per set, exactly like the scalar field.
        scaled = copy.deepcopy(p)
        scaled["steps"][0]["rateSets"][0]["direction"] = [0.0, 0.5, 0.0]
        self.assertEqual(baseline, requested_input_hash(validate_project(scaled)[0]))

        # A set without a directional velocity cannot consume direction/visibility.
        zero = copy.deepcopy(p)
        zero["steps"][0]["rateSets"][0].update(directionalVelocity=0.0, isotropicVelocity=0.0)
        zero_other = copy.deepcopy(zero)
        zero_other["steps"][0]["rateSets"][0].update(direction=[1.0, 0.0, 0.0],
                                                     calculateVisibility=False)
        self.assertEqual(requested_input_hash(validate_project(zero)[0]),
                         requested_input_hash(validate_project(zero_other)[0]))

        changed = copy.deepcopy(p)
        changed["steps"][0]["rateSets"][1]["isotropicVelocity"] = 0.02
        self.assertNotEqual(baseline, requested_input_hash(validate_project(changed)[0]))

        def bad(mutate, label):
            value = copy.deepcopy(p)
            mutate(value)
            with self.assertRaises(ProjectInputError, msg=label):
                validate_project(value)

        bad(lambda value: value["steps"][0].update(rateSets=[]), "empty list")
        bad(lambda value: value["steps"][0].update(
            rateSets=[copy.deepcopy(value["steps"][0]["rateSets"][0]) for _ in range(9)]), "too many sets")
        bad(lambda value: value["steps"][0]["rateSets"][0].update(direction=[0.0, 0.0, 0.0]),
            "zero direction")
        bad(lambda value: value["steps"][0]["rateSets"][0].update(direction=[0.0, 1.0, 0.5]),
            "2D third component")
        # A raw third component within 1e-12 can still normalize to a mostly
        # out-of-plane beam; the 2D check must use the normalized direction.
        bad(lambda value: value["steps"][0]["rateSets"][0].update(direction=[0.0, -1e-12, 1e-12]),
            "2D rate-set normalized third component")
        bad(lambda value: value["steps"][0].update(rateSets=None, direction=[0.0, -1e-12, 1e-12],
                                                   directionalRateMagnitude=0.1,
                                                   isotropicRateMagnitude=0.0),
            "2D scalar normalized third component")
        # An in-plane vector with a negligible normalized z stays legal.
        tiny = copy.deepcopy(p)
        tiny["steps"][0]["rateSets"][0]["direction"] = [0.0, 1.0, 1e-13]
        validate_project(tiny)
        tiny_scalar = copy.deepcopy(p)
        tiny_scalar["steps"][0].update(rateSets=None, direction=[0.0, 1.0, 1e-13],
                                       directionalRateMagnitude=0.1, isotropicRateMagnitude=0.0)
        validate_project(tiny_scalar)
        bad(lambda value: value["steps"][0]["rateSets"][0].update(directionalVelocity=-0.1),
            "negative rate")
        bad(lambda value: value["steps"][0]["rateSets"][0].update(isotropicVelocity=2.5),
            "rate above bound")
        bad(lambda value: value["steps"][0].update(direction=[0.0, -1.0, 0.0]), "scalar direction")
        bad(lambda value: value["steps"][0].update(directionalRateMagnitude=0.1),
            "scalar directional rate")
        bad(lambda value: value["steps"][0].update(isotropicRateMagnitude=0.1),
            "scalar isotropic rate")
        bad(lambda value: value["steps"][0].update(calculateVisibility=False),
            "step-level visibility")
        bad(lambda value: value["steps"][0]["rateSets"][0].update(maskMaterials=["Mask", "Mask"]),
            "duplicate masks")
        bad(lambda value: value["steps"][0]["rateSets"][0].update(maskMaterials=["NotAMaterial"]),
            "unknown mask")
        bad(lambda value: value["steps"][0].update(rateFunction="lambda x: x"), "custom callable")

        # The mutual-exclusion 422 tells an unchanged scalar-mode consumer how
        # to branch instead of materializing absent fields as zero.
        regressed = copy.deepcopy(p)
        regressed["steps"][0].update(direction=[0.0, 0.0, 0.0],
                                     directionalRateMagnitude=0.0,
                                     isotropicRateMagnitude=0.0)
        with self.assertRaises(ProjectInputError) as raised:
            validate_project(regressed)
        message = " ".join(issue.message for issue in raised.exception.issues)
        self.assertIn("omit all three fields in rateSets mode", message)
        self.assertIn("never as 0 placeholders", message)
        self.assertIn("rateSets", message)
        self.assertTrue(any(issue.path.endswith(".directional") for issue in raised.exception.issues),
                        [issue.path for issue in raised.exception.issues])

        # A mask list that drops geometry protection is explicit and warned about.
        masked = project(2, "trench")
        masked["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, 1.0, 0.0], "directionalVelocity": 0.2, "maskMaterials": []}])]
        _parsed, masked_warnings = validate_project(masked)
        self.assertTrue(any("omits geometry mask material(s) Mask" in warning
                            for warning in masked_warnings), masked_warnings)

        # 3D uses all three components; the same request is rejected in 2D.
        three_d = project(3)
        three_d["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, 0.0, 1.0], "directionalVelocity": 0.2}])]
        validate_project(three_d)
        planar = project(2)
        planar["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, 0.0, 1.0], "directionalVelocity": 0.2}])]
        with self.assertRaises(ProjectInputError):
            validate_project(planar)

    def test_zero_directional_and_isotropic_components_are_noop_independently(self):
        p = project(2)
        step = directional_step(directional=0.0, isotropic=0.0)
        p["steps"] = [step]
        first = requested_input_hash(validate_project(p)[0])
        q = copy.deepcopy(p)
        q["steps"][0]["direction"] = [1.0, 0.0, 0.0]
        q["steps"][0]["calculateVisibility"] = False
        self.assertEqual(first, requested_input_hash(validate_project(q)[0]))
        q["steps"][0]["isotropicRateMagnitude"] = 0.1
        self.assertNotEqual(first, requested_input_hash(validate_project(q)[0]))


class DirectionalWorkflow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id):
        for _ in range(600):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded acceptance timeout")

    async def execute(self, value):
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    async def reference(self, value, label):
        input_path = EVIDENCE / f"{label}-input.json"
        output_path = EVIDENCE / f"{label}-direct-reference.json"
        guard_path = EVIDENCE / f"{label}-reference-guard.json"
        log_path = EVIDENCE / f"{label}-reference.log"
        input_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        command = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/bounded_child.py"),
                   "--cwd", str(ROOT), "--record", str(guard_path), "--log-path", str(log_path),
                   "--timeout-seconds", "60", "--max-rss-mib", "2048", "--max-log-bytes", "2097152", "--",
                   str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/directional_direct_reference.py"),
                   str(input_path), str(output_path)]
        completed = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL)
        self.assertEqual(await completed.wait(), 0, log_path.read_text(encoding="utf-8", errors="replace"))
        return json.loads(output_path.read_text(encoding="utf-8"))

    async def assert_reference(self, value, label):
        run_id, info = await self.execute(value)
        direct = await self.reference(value, label)
        key = "lines" if value["dimension"] == 2 else "triangles"
        errors = []
        manifest = storage.read_json(storage.run_path(run_id) / "manifest.json")
        self.assertEqual(manifest["schemaVersion"], 5)
        self.assertIsNone(manifest["actualSeed"], "DirectionalProcess must not receive a random seed")
        self.assertEqual(manifest["plannedExecution"], [{"stepId": s["id"], "engine": "not-applicable", "seed": None} for s in value["steps"]])
        self.assertTrue(any(item["model"] == "directional" for item in info["actualExecution"]))
        for row in info["snapshots"]:
            snapshot_store.ensure_materialized(storage.run_path(run_id), row['stepId'])
            snapshot = storage.read_json(storage.run_path(run_id) / f"{row['stepId']}.json")
            expected = direct[row["stepId"]]
            self.assertEqual(snapshot[key], expected["cells"])
            self.assertEqual(snapshot["cellData"]["materialIds"], expected["materialIds"])
            self.assertEqual(snapshot["materialIdsByLevelSetIndex"], expected["materialMap"])
            self.assertEqual(len(snapshot["points"]), len(expected["points"]))
            error = max((math.dist(a, b) for a, b in zip(snapshot["points"], expected["points"])), default=0.0)
            self.assertLessEqual(error, 1e-10)
            errors.append(error)
        comparison = {"runId": run_id, "dimension": value["dimension"], "schemaVersion": 5,
                      "nativeTopologyAndMaterialArraysEqual": True, "maxCoordinateErrors": errors,
                      "coordinateTolerance": 1e-10, "actualExecution": info["actualExecution"]}
        (EVIDENCE / f"{label}-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
        return run_id, info

    async def test_real_2d_etch_3d_deposit_and_visibility(self):
        two_d = project(2)
        two_d["steps"] = [directional_step(direction=[0.0, -1.0, 0.0], directional=0.3, isotropic=0.1)]
        run_2d, info_2d = await self.assert_reference(two_d, "directional-etch-2d")
        record_2d = info_2d["actualExecution"][0]
        self.assertEqual(record_2d["actualEngine"], "not-applicable")
        self.assertEqual(record_2d["actualSeed"], None)
        self.assertEqual(record_2d["actualParameters"]["nativeDirectionalVelocity"], -0.3)
        self.assertEqual(record_2d["actualParameters"]["nativeIsotropicVelocity"], -0.1)
        self.assertEqual(record_2d["actualParameters"]["raysPerPoint"], "not-applicable")
        self.assertEqual(record_2d["actualParameters"]["nativeUnitDirection"], [0.0, -1.0, 0.0])

        three_d = project(3)
        three_d["steps"] = [directional_step(operation="deposit", direction=[0.0, 0.0, -1.0], directional=0.25,
                                             isotropic=0.05, deposit_material="W")]
        run_3d, info_3d = await self.assert_reference(three_d, "directional-deposit-3d")
        parameters = info_3d["actualExecution"][0]["actualParameters"]
        self.assertEqual(parameters["nativeDirectionalVelocity"], 0.25)
        self.assertEqual(parameters["nativeIsotropicVelocity"], 0.05)
        self.assertIn(70, storage.read_json(storage.run_path(run_3d) / "manifest.json")["snapshots"][-1]["materialIds"])

        visibility_outputs = {}
        for dimension in (2, 3):
            outputs = {}
            for visibility in (True, False):
                value = project(dimension, "trench")
                value["steps"] = [directional_step(direction=[0.6, -0.8, 0.0] if dimension == 2 else [0.6, 0.0, -0.8],
                                                    directional=0.5, isotropic=0.0, visibility=visibility,
                                                    step_id=f"step_vis_{dimension}_{int(visibility)}")]
                label = f"directional-visibility-{dimension}d-{str(visibility).lower()}"
                run_id, info = await self.assert_reference(value, label)
                record = info["actualExecution"][0]
                self.assertEqual(record["actualParameters"]["calculateVisibility"], visibility)
                self.assertEqual(record["actualParameters"]["maskMaterials"], ["Mask"])
                snapshot_store.ensure_materialized(storage.run_path(run_id), info["snapshots"][-1]["stepId"])
                outputs[visibility] = storage.read_json(storage.run_path(run_id) / info["snapshots"][-1]["snapshot"])
            key = "lines" if dimension == 2 else "triangles"
            self.assertNotEqual(outputs[True][key], outputs[False][key], f"visibility toggle did not affect {dimension}D geometry")
            visibility_outputs[str(dimension)] = {"onPointCount": len(outputs[True]["points"]), "offPointCount": len(outputs[False]["points"])}
        (EVIDENCE / "visibility-effect.json").write_text(json.dumps(visibility_outputs, indent=2) + "\n", encoding="utf-8")

    async def test_save_reopen_project_export_replay_history_and_zero_noop(self):
        value = project(2)
        value["steps"] = [directional_step(directional=0.2, isotropic=0.0),
                          directional_step(directional=0.0, isotropic=0.0, step_id="step_directional_zero")]
        # The second step is a true no-op and should not duplicate/modify a material level set.
        run_id, info = await self.assert_reference(value, "directional-save-replay-2d")
        created = await asgi_request("/api/projects", method="POST", payload=value)
        self.assertEqual(created[0], 201, created[2])
        project_id = json_body(created)["id"]
        reopened = json_body(await asgi_request(f"/api/projects/{project_id}"))["project"]
        self.assertEqual(reopened, value)
        validated = await asgi_request("/api/projects/validate", method="POST", payload=reopened)
        self.assertEqual(validated[0], 200, validated[2])
        self.assertEqual(json_body(validated)["normalizedProject"], value)

        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["schemaVersion"], 5)
        self.assertEqual(replay["executionRequirements"]["steps"], [])
        self.assertEqual(replay["normalizedProject"]["steps"][1]["directionalRateMagnitude"], 0.0)
        submitted = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(submitted[0], 202, submitted[2])
        replay_id = json_body(submitted)["runId"]
        replay_info = await self.wait(replay_id)
        for step in ("initial", "step_directional", "step_directional_zero"):
            snapshot_store.ensure_materialized(storage.run_path(run_id), step)
            snapshot_store.ensure_materialized(storage.run_path(replay_id), step)
            left = storage.read_json(storage.run_path(run_id) / f"{step}.json")
            right = storage.read_json(storage.run_path(replay_id) / f"{step}.json")
            self.assertEqual(left["points"], right["points"])
            self.assertEqual(left.get("lines"), right.get("lines"))
            self.assertEqual(left.get("triangles"), right.get("triangles"))
            self.assertEqual(left["cellData"]["materialIds"], right["cellData"]["materialIds"])
        snapshot_store.ensure_materialized(storage.run_path(run_id), "step_directional_zero")
        self.assertTrue(storage.read_json(storage.run_path(run_id) / "step_directional_zero.json")["noOp"])
        self.assertTrue(replay_info["actualExecution"][-1]["noOp"])
        self.assertIn("not-applicable", replay_info["actualExecution"][-1]["actualParameters"]["calculateVisibility"])

        archive = await asgi_request(f"/api/runs/{run_id}/download")
        self.assertEqual(archive[0], 200)
        with zipfile.ZipFile(io.BytesIO(archive[2])) as zipped:
            self.assertIn("replay-project.json", zipped.namelist())
            self.assertIn("step_directional.json", zipped.namelist())
        history = json_body(await asgi_request(f"/api/runs/{run_id}"))
        self.assertEqual(history["actualExecution"][0]["actualParameters"]["nativeDirectionalVelocity"], -0.2)
        self.assertEqual(history["actualExecution"][0]["actualParameters"]["randomSeed"], "not-applicable")

    async def test_rate_sets_native_reference_in_2d_and_3d(self):
        two_d = project(2)
        two_d["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.15, "isotropicVelocity": 0.05},
            {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.1},
        ])]
        run_id, info = await self.assert_reference(two_d, "directional-ratesets-2d")
        record = info["actualExecution"][0]
        parameters = record["actualParameters"]
        self.assertEqual(record["actualEngine"], "not-applicable")
        self.assertIsNone(record["actualSeed"])
        self.assertIsNone(record["actualRate"])
        self.assertEqual(parameters["mode"], "rateSets")
        self.assertEqual(parameters["nativeOverload"], "DirectionalProcess(rateSets: Sequence[RateSet])")
        self.assertEqual(parameters["rateSetCount"], 2)
        self.assertEqual([item["nativeDirectionalVelocity"] for item in parameters["rateSets"]],
                         [-0.15, -0.1])
        self.assertEqual([item["nativeIsotropicVelocity"] for item in parameters["rateSets"]],
                         [-0.05, -0.0])
        self.assertEqual([item["nativeUnitDirection"] for item in parameters["rateSets"]],
                         [[0.0, -1.0, 0.0], [0.6, -0.8, 0.0]])
        self.assertEqual([item["maskSource"] for item in parameters["rateSets"]],
                         ["geometry", "geometry"])
        self.assertEqual(parameters["rateSets"][0]["requestedMaskMaterials"], None)
        self.assertEqual(parameters["maskMaterials"], [])
        self.assertEqual(parameters["raysPerPoint"], "not-applicable")
        self.assertEqual(parameters["randomSeed"], "not-applicable")
        self.assertEqual(storage.read_json(storage.run_path(run_id) / "manifest.json")["schemaVersion"], 5)

        three_d = project(3)
        three_d["steps"] = [rate_set_step(operation="deposit", deposit_material="W", rate_sets=[
            {"direction": [0.0, 0.0, -1.0], "directionalVelocity": 0.2, "isotropicVelocity": 0.02},
            {"direction": [0.6, 0.0, -0.8], "directionalVelocity": 0.05},
        ])]
        run_3d, info_3d = await self.assert_reference(three_d, "directional-ratesets-3d")
        parameters_3d = info_3d["actualExecution"][0]["actualParameters"]
        self.assertEqual([item["nativeDirectionalVelocity"] for item in parameters_3d["rateSets"]],
                         [0.2, 0.05])
        self.assertEqual(parameters_3d["rateSets"][1]["nativeIsotropicVelocity"], 0.0)
        self.assertIn(70, storage.read_json(storage.run_path(run_3d) / "manifest.json")["snapshots"][-1]["materialIds"])

    async def test_rate_sets_mixed_visibility_and_masks_match_native_reference(self):
        # Both sets used to share one visibility toggle, which a single global
        # flag could satisfy. The reference fixture builds one RateSet per entry
        # from the request, so a mixed True/False pair must match only if the
        # worker also applies visibility per set.
        for dimension in (2, 3):
            value = project(dimension, "trench")
            positive = [0.6, -0.8, 0.0] if dimension == 2 else [0.6, 0.0, -0.8]
            value["steps"] = [rate_set_step(rate_sets=[
                {"direction": positive, "directionalVelocity": 0.3, "calculateVisibility": True},
                {"direction": [-component for component in positive],
                 "directionalVelocity": 0.3, "calculateVisibility": False},
            ])]
            label = f"directional-ratesets-visibility-mixed-{dimension}d"
            _run_id, info = await self.assert_reference(value, label)
            self.assertEqual([item["calculateVisibility"] for item in
                              info["actualExecution"][0]["actualParameters"]["rateSets"]],
                             [True, False])

        # Per-set masks, mixed: one set protects the geometry Mask and the other
        # explicitly acts on it. A global mask list cannot produce this result.
        value = project(2, "trench")
        value["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.2,
             "maskMaterials": ["Mask"]},
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.2,
             "maskMaterials": []},
        ])]
        _run_id, info = await self.assert_reference(value, "directional-ratesets-mask-mixed-2d")
        parameters = info["actualExecution"][0]["actualParameters"]
        self.assertEqual([item["maskMaterials"] for item in parameters["rateSets"]],
                         [["Mask"], []])
        self.assertEqual([item["maskSource"] for item in parameters["rateSets"]],
                         ["request", "request"])
        self.assertEqual([item["maskMaterialIds"] for item in parameters["rateSets"]],
                         [[0], []])

    async def test_rate_sets_additivity_per_set_mask_and_visibility(self):
        # Two 0.15 sets in the same direction are the scalar 0.3 field: the
        # native sequence overload sums each RateSet contribution.
        scalar = project(2, "trench")
        scalar["steps"] = [directional_step(direction=[0.0, -1.0, 0.0], directional=0.3,
                                             isotropic=0.0, visibility=True)]
        scalar_id, _ = await self.execute(scalar)
        split = project(2, "trench")
        split["steps"] = [rate_set_step(duration=0.4, rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.15},
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.15},
        ])]
        split_id, split_info = await self.execute(split)
        self.assertEqual([item["maskMaterials"] for item in split_info["actualExecution"][0]["actualParameters"]["rateSets"]],
                         [["Mask"], ["Mask"]])
        snapshot_store.ensure_materialized(storage.run_path(scalar_id), "step_directional")
        snapshot_store.ensure_materialized(storage.run_path(split_id), "step_rate_sets")
        scalar_snapshot = storage.read_json(storage.run_path(scalar_id) / "step_directional.json")
        split_snapshot = storage.read_json(storage.run_path(split_id) / "step_rate_sets.json")
        self.assertEqual(len(scalar_snapshot["points"]), len(split_snapshot["points"]))
        self.assertEqual(scalar_snapshot["lines"], split_snapshot["lines"])
        self.assertEqual(scalar_snapshot["cellData"]["materialIds"],
                         split_snapshot["cellData"]["materialIds"])
        self.assertLessEqual(max((math.dist(left, right) for left, right in
                                  zip(scalar_snapshot["points"], split_snapshot["points"])), default=0.0),
                             1e-12)

        # Per-set masks: dropping the geometry mask for one set changes the field.
        masked = project(2, "trench")
        masked["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.2,
             "maskMaterials": ["Mask"]},
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.2,
             "maskMaterials": ["Mask"]},
        ])]
        unmasked = copy.deepcopy(masked)
        unmasked["steps"][0]["rateSets"][1]["maskMaterials"] = []
        masked_id, masked_info = await self.execute(masked)
        unmasked_id, unmasked_info = await self.execute(unmasked)
        self.assertEqual([item["maskSource"] for item in
                          unmasked_info["actualExecution"][0]["actualParameters"]["rateSets"]],
                         ["request", "request"])
        self.assertEqual([item["maskMaterials"] for item in
                          unmasked_info["actualExecution"][0]["actualParameters"]["rateSets"]],
                         [["Mask"], []])
        self.assertEqual([item["maskMaterialIds"] for item in
                          unmasked_info["actualExecution"][0]["actualParameters"]["rateSets"]],
                         [[0], []])
        compare_mask = []
        for run_id in (masked_id, unmasked_id):
            snapshot_store.ensure_materialized(storage.run_path(run_id), "step_rate_sets")
            compare_mask.append(storage.read_json(storage.run_path(run_id) / "step_rate_sets.json"))
        self.assertNotEqual(compare_mask[0]["points"], compare_mask[1]["points"])

        # Per-set visibility: the diagonal set shadows differently when it is off.
        outputs = {}
        for visibility in (True, False):
            value = project(2, "trench")
            value["steps"] = [rate_set_step(rate_sets=[
                {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.3,
                 "calculateVisibility": visibility},
                {"direction": [-0.6, -0.8, 0.0], "directionalVelocity": 0.3,
                 "calculateVisibility": visibility},
            ])]
            run_id, info = await self.execute(value)
            self.assertEqual([item["calculateVisibility"] for item in
                              info["actualExecution"][0]["actualParameters"]["rateSets"]],
                             [visibility, visibility])
            snapshot_store.ensure_materialized(storage.run_path(run_id), "step_rate_sets")
            outputs[visibility] = storage.read_json(storage.run_path(run_id) / "step_rate_sets.json")
        self.assertNotEqual(outputs[True]["points"], outputs[False]["points"],
                            "per-set visibility did not reach the native RateSet")

    async def test_rate_sets_save_reopen_replay_and_zero_noop(self):
        value = project(2)
        value["steps"] = [rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.12, "isotropicVelocity": 0.02},
            {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.08},
        ]), rate_set_step(rate_sets=[
            {"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.0,
             "isotropicVelocity": 0.0},
        ], step_id="step_rate_sets_zero")]
        run_id, info = await self.assert_reference(value, "directional-ratesets-save-replay-2d")
        zero_parameters = info["actualExecution"][-1]["actualParameters"]
        self.assertTrue(info["actualExecution"][-1]["noOp"])
        self.assertEqual(zero_parameters["mode"], "rateSets")
        self.assertEqual(zero_parameters["rateSetCount"], 1)
        self.assertIn("not-applicable", zero_parameters["rateSets"][0]["calculateVisibility"])
        snapshot_store.ensure_materialized(storage.run_path(run_id), "step_rate_sets_zero")
        self.assertTrue(storage.read_json(storage.run_path(run_id) / "step_rate_sets_zero.json")["noOp"])

        created = await asgi_request("/api/projects", method="POST", payload=value)
        self.assertEqual(created[0], 201, created[2])
        project_id = json_body(created)["id"]
        reopened = json_body(await asgi_request(f"/api/projects/{project_id}"))["project"]
        normalized = validate_project(value)[0].model_dump(mode="json", exclude_none=True)
        self.assertEqual(reopened, normalized)
        self.assertEqual([item["direction"] for item in reopened["steps"][0]["rateSets"]],
                         [item["direction"] for item in value["steps"][0]["rateSets"]])
        self.assertEqual([item["directionalVelocity"] for item in reopened["steps"][0]["rateSets"]],
                         [item["directionalVelocity"] for item in value["steps"][0]["rateSets"]])
        self.assertTrue(all(item["calculateVisibility"] is True
                            for item in reopened["steps"][0]["rateSets"]))
        validated = await asgi_request("/api/projects/validate", method="POST", payload=reopened)
        self.assertEqual(validated[0], 200, validated[2])
        self.assertEqual(json_body(validated)["normalizedProject"], normalized)
        self.assertEqual(json_body(validated)["requestedInputHash"],
                         requested_input_hash(validate_project(value)[0]))

        # An unchanged scalar-mode consumer writing back zero placeholders must
        # get the documented 422 instead of a silent mode switch.
        regressed = copy.deepcopy(reopened)
        regressed["steps"][0].update(direction=[0.0, 0.0, 0.0],
                                     directionalRateMagnitude=0.0,
                                     isotropicRateMagnitude=0.0)
        rejected = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": regressed})
        self.assertEqual(rejected[0], 422, rejected[2])
        detail = json_body(rejected)["detail"]
        self.assertTrue(any("omit all three fields in rateSets mode" in item["message"]
                            for item in detail), detail)
        self.assertTrue(all(item["path"].endswith("steps[0].directional") for item in detail), detail)
        validate_rejected = await asgi_request("/api/projects/validate", method="POST", payload=regressed)
        self.assertEqual(validate_rejected[0], 422, validate_rejected[2])

        replay = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        self.assertEqual(replay["schemaVersion"], 5)
        self.assertEqual(replay["executionRequirements"]["steps"], [])
        self.assertEqual([item["direction"] for item in replay["normalizedProject"]["steps"][0]["rateSets"]],
                         [item["direction"] for item in value["steps"][0]["rateSets"]])
        self.assertEqual([item["directionalVelocity"] for item in replay["normalizedProject"]["steps"][0]["rateSets"]],
                         [item["directionalVelocity"] for item in value["steps"][0]["rateSets"]])
        submitted = await asgi_request("/api/runs/replay", method="POST", payload=replay)
        self.assertEqual(submitted[0], 202, submitted[2])
        replay_id = json_body(submitted)["runId"]
        replay_info = await self.wait(replay_id)
        for step in ("initial", "step_rate_sets", "step_rate_sets_zero"):
            snapshot_store.ensure_materialized(storage.run_path(run_id), step)
            snapshot_store.ensure_materialized(storage.run_path(replay_id), step)
            left = storage.read_json(storage.run_path(run_id) / f"{step}.json")
            right = storage.read_json(storage.run_path(replay_id) / f"{step}.json")
            self.assertEqual(left["points"], right["points"])
            self.assertEqual(left.get("lines"), right.get("lines"))
            self.assertEqual(left.get("triangles"), right.get("triangles"))
            self.assertEqual(left["cellData"]["materialIds"], right["cellData"]["materialIds"])
        self.assertEqual(replay_info["actualExecution"][0]["actualParameters"]["rateSets"],
                         info["actualExecution"][0]["actualParameters"]["rateSets"])

        archive = await asgi_request(f"/api/runs/{run_id}/download")
        self.assertEqual(archive[0], 200)
        with zipfile.ZipFile(io.BytesIO(archive[2])) as zipped:
            self.assertIn("replay-project.json", zipped.namelist())
            self.assertIn("step_rate_sets.json", zipped.namelist())
