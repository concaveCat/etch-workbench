"""Isotropic dispatch and isotropic material-rate contract tests.

These tests pin the behaviour described in the task contract for the optional
``isotropicMethod`` / ``isotropicTimeStepRatio`` step fields and for the
deposition-capable ``rateMode=material`` isotropic table.  They are additive:
a project that omits every new field must keep its normalized JSON and its
request hash, and a step snapshot/preview must still exist for every run.

Native runs use the normal API -> bounded worker path with a small 2D/3D
plane or trench and a coarse grid; no GPU is involved.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import unittest
from pathlib import Path

from backend import snapshots as snapshot_store
from backend import storage
from backend.hashing import requested_input_hash
from backend.main import app
from backend.validation import ProjectInputError, validate_project
from tests.test_api_integration import asgi_request, json_body


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests" / "fixtures" / "legacy"
REFERENCE_DIR = storage.VAR / "test-iso-rates"

# Baseline hashes captured from the pre-feature commit (git HEAD before the
# isotropic dispatch fields existed).  The new fields are optional and must be
# omitted from the normalized/hash input when absent.
FROZEN_LEGACY_HASHES = {
    (2, "deposit"): "cac6d83d04d20f2c6d59d4b391452a23f8f9fc242a8558bf0980f773d5e3a79e",
    (2, "etch"): "465091bfcb6f6cde4cc2d9c5a4846fc23d9ce71f5744f53ef06e94d81b24aa00",
    (3, "deposit"): "edd0414930807bd1fedd0fa81fb0ef77c16cde68dc1808969bf249ac6796b3c2",
    (3, "etch"): "4ce827e9d90dc82a27af2851628a9bafc66b94b1ee83683631b421b0ba4c9c15",
}


def project(dimension: int = 2, geometry: str = "plane") -> dict:
    value = {
        "schemaVersion": 5,
        "name": f"各向同性调度 {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2
                      else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 314159},
                   "enginePreference": "cpu", "timeoutSeconds": 30},
    }
    if geometry == "trench":
        value["geometry"] = {
            "kind": "trench", "width": 1.6, "depth": 1.2, "taperAngle": 0.0,
            "maskHeight": 0.6, "maskTaperAngle": 0.0,
            "substrateMaterial": "Si", "maskMaterial": "Mask",
        }
    if dimension == 2:
        value["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        value["view3D"] = {
            "projection": "parallel", "camera": None, "displayMode": "surface",
            "visibleMaterialIds": [], "opacityByMaterial": {},
            "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
            "section": {"enabled": False, "axis": "y", "position": 0.0},
        }
    return value


def iso_step(operation: str = "deposit", *, method=None, ratio=None,
             rate: float = 0.3, duration: float = 1.0,
             step_id: str | None = None, **extra) -> dict:
    step = {
        "id": step_id or ("step_iso_deposit" if operation == "deposit" else "step_iso_etch"),
        "name": "各向同性",
        "enabled": True,
        "operation": operation,
        "model": "isotropic",
        "rateMagnitude": rate,
        "duration": duration,
        "rateMode": "uniform",
    }
    if operation == "deposit":
        step["depositMaterial"] = "SiO2"
    if method is not None:
        step["isotropicMethod"] = method
    if ratio is not None:
        step["isotropicTimeStepRatio"] = ratio
    step.update(extra)
    return step


def particle_step(**extra) -> dict:
    return {
        "id": "step_particle_iso",
        "name": "单粒子",
        "enabled": True,
        "operation": "etch",
        "model": "singleParticle",
        "rateMagnitude": 0.2,
        "duration": 0.4,
        "stickingProbability": 0.5,
        "sourceExponent": 1.0,
        **extra,
    }


def vertical_axis(dimension: int) -> int:
    return 1 if dimension == 2 else 2


def material_vertices(snapshot: dict, material_id: int) -> set[int]:
    cells = snapshot.get("triangles") or snapshot.get("lines") or []
    ids = snapshot["cellData"]["materialIds"]
    return {vertex for cell, current in zip(cells, ids)
            if int(current) == int(material_id) for vertex in cell}


def material_top(snapshot: dict, material_id: int, axis: int) -> float | None:
    vertices = material_vertices(snapshot, material_id)
    if not vertices:
        return None
    return max(float(snapshot["points"][index][axis]) for index in vertices)


def exposed_floor(snapshot: dict, material_id: int, axis: int, is_3d: bool) -> float | None:
    """Top of a material restricted to the central opening of a trench."""
    vertices = material_vertices(snapshot, material_id)
    if not vertices:
        return None
    selected = []
    for index in vertices:
        point = snapshot["points"][index]
        if abs(float(point[0])) > 0.4:
            continue
        if is_3d and abs(float(point[1])) > 0.4:
            continue
        selected.append(float(point[axis]))
    return max(selected) if selected else None


class IsotropicDispatchSchemaTests(unittest.TestCase):
    def test_absent_fields_keep_frozen_hashes_and_legacy_fixture(self):
        for dimension in (2, 3):
            for operation in ("deposit", "etch"):
                with self.subTest(dimension=dimension, operation=operation):
                    value = project(dimension)
                    value["steps"] = [iso_step(operation)]
                    parsed, _ = validate_project(value)
                    normalized = parsed.model_dump(mode="json", exclude_none=True)
                    self.assertEqual(requested_input_hash(parsed),
                                     FROZEN_LEGACY_HASHES[(dimension, operation)])
                    self.assertNotIn("isotropicMethod", normalized["steps"][0])
                    self.assertNotIn("isotropicTimeStepRatio", normalized["steps"][0])

                    explicit = copy.deepcopy(value)
                    explicit["steps"][0]["isotropicMethod"] = "levelSet"
                    self.assertNotEqual(requested_input_hash(validate_project(explicit)[0]),
                                        FROZEN_LEGACY_HASHES[(dimension, operation)])
                    explicit = copy.deepcopy(value)
                    explicit["steps"][0]["isotropicTimeStepRatio"] = 0.1
                    self.assertNotEqual(requested_input_hash(validate_project(explicit)[0]),
                                        FROZEN_LEGACY_HASHES[(dimension, operation)])

        # The legacy fixture is read-only input: validating it and a deep copy
        # must produce the same canonical JSON and the archived hash, and the
        # file itself must stay byte-identical.
        fixture = LEGACY / "d77157dbeec7f08a3cba5622" / "project.json"
        before = fixture.read_bytes()
        archived = json.loads((LEGACY / "index.json").read_text(encoding="utf-8"))
        expected = next(row["requestedInputHash"] for row in archived
                        if row["runId"] == "d77157dbeec7f08a3cba5622")
        raw = json.loads(before)
        parsed, _ = validate_project(raw)
        copied, _ = validate_project(copy.deepcopy(raw))
        self.assertEqual(parsed.model_dump(mode="json", exclude_none=True),
                         copied.model_dump(mode="json", exclude_none=True))
        self.assertEqual(requested_input_hash(parsed), expected)
        self.assertEqual(fixture.read_bytes(), before)

    def test_dispatch_fields_accept_the_documented_values(self):
        cases = [
            (2, "plane", "deposit", {"method": "auto"}),
            (2, "plane", "deposit", {"method": "geometric"}),
            (2, "trench", "etch", {"method": "geometric"}),
            (2, "plane", "etch", {"method": "levelSet", "ratio": 0.05}),
            (2, "plane", "etch", {"method": "levelSet", "ratio": 0.5}),
            (3, "plane", "deposit", {"method": "auto"}),
            (3, "plane", "deposit", {"method": "geometric"}),
            (3, "plane", "etch", {"method": "levelSet", "ratio": 0.1}),
        ]
        for dimension, geometry, operation, options in cases:
            with self.subTest(dimension=dimension, geometry=geometry,
                              operation=operation, options=options):
                value = project(dimension, geometry)
                value["steps"] = [iso_step(operation, **options)]
                parsed, _ = validate_project(value, kind="simulate")
                step = parsed.model_dump(mode="json", exclude_none=True)["steps"][0]
                self.assertEqual(step["isotropicMethod"], options["method"])
                if "ratio" in options:
                    self.assertEqual(float(step["isotropicTimeStepRatio"]), options["ratio"])

        # ``auto`` is also legal for a material-rate table; it must simply not
        # select the geometric path.  Validation is the only part checked here.
        value = project(2, "trench")
        value["steps"] = [iso_step("etch", method="auto", rateMode="material",
                                   rateMagnitude=0.0, materialRates={"Si": 0.2})]
        validate_project(value, kind="simulate")

    def test_dispatch_fields_reject_non_isotropic_models_and_bad_ratios(self):
        for extra in ({"isotropicMethod": "auto"},
                      {"isotropicTimeStepRatio": 0.1}):
            with self.subTest(extra=extra):
                value = project(2)
                value["steps"] = [particle_step(**extra)]
                with self.assertRaises(ProjectInputError):
                    validate_project(value, kind="simulate")

        for ratio in (0.049, 0.51, -0.1, 1.0):
            with self.subTest(ratio=ratio):
                value = project(2)
                value["steps"] = [iso_step("etch", method="levelSet", ratio=ratio)]
                with self.assertRaises(ProjectInputError):
                    validate_project(value, kind="simulate")

        value = project(2)
        value["steps"] = [iso_step("etch", method="sideways")]
        with self.assertRaises(ProjectInputError):
            validate_project(value, kind="simulate")

    def test_geometric_validation_rejects_unsafe_requests(self):
        unsafe = []
        value = project(2, "trench")
        value["steps"] = [iso_step("deposit", method="geometric")]
        unsafe.append(("deposition with geometry mask", value))

        value = project(2)
        value["steps"] = [iso_step("etch", method="geometric", rateMode="material",
                                   rateMagnitude=0.0, materialRates={"Si": 0.2})]
        unsafe.append(("material rate mode", value))

        value = project(2)
        value["steps"] = [iso_step("etch", method="geometric", rate=0.1, duration=1.0)]
        unsafe.append(("sub-grid thickness", value))

        for label, value in unsafe:
            with self.subTest(label=label):
                with self.assertRaises(ProjectInputError):
                    validate_project(value, kind="simulate")

        # Explicit geometric in 3D remains an allowed override, but the
        # contract requires a visible warning that it is usually slower.
        value = project(3)
        value["steps"] = [iso_step("deposit", method="geometric")]
        _parsed, warnings = validate_project(value, kind="simulate")
        text = " ".join(warnings).lower()
        self.assertTrue(any("geometric" in warning.lower() or "几何" in warning
                            for warning in warnings), warnings)
        self.assertTrue("slow" in text or "3d" in text or "慢" in text, warnings)

    def test_isotropic_material_rate_deposit_accepts_and_validates_materials(self):
        # Deposition used to be rejected for rateMode=material; the table is
        # now valid for both operations.
        value = project(2, "trench")
        value["steps"] = [iso_step("deposit", rateMode="material", rateMagnitude=0.0,
                                   materialRates={"Si": 0.2})]
        parsed, _ = validate_project(value, kind="simulate")
        self.assertEqual(parsed.steps[0].rateMode, "material")

        for rates, label in (
            ({"NotAMaterial": 0.1}, "unknown material"),
            ({"Si3N4": 0.1}, "catalog material that is not present"),
        ):
            with self.subTest(label=label):
                value = project(2)
                value["steps"] = [iso_step("deposit", rateMode="material",
                                           rateMagnitude=0.0, materialRates=rates)]
                with self.assertRaises(ProjectInputError):
                    validate_project(value, kind="simulate")


class IsotropicDispatchWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded the acceptance timeout")

    async def execute(self, value: dict) -> tuple[str, dict]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    def snapshot(self, run_id: str, step_id: str) -> dict:
        path = snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(path)

    def assert_every_step_has_a_snapshot(self, run_id: str, info: dict) -> None:
        self.assertGreaterEqual(len(info["snapshots"]), 2, info["snapshots"])
        for row in info["snapshots"]:
            snapshot = self.snapshot(run_id, row["stepId"])
            self.assertEqual(snapshot["stepId"], row["stepId"])
            self.assertTrue(snapshot["points"], row["stepId"])

    def last_parameters(self, info: dict) -> dict:
        return info["actualExecution"][-1]["actualParameters"]

    async def test_2d_auto_selects_geometric_and_keeps_a_snapshot(self):
        value = project(2)
        value["steps"] = [iso_step("deposit", method="auto", rate=0.3, duration=1.0)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["requestedIsotropicMethod"], "auto")
        self.assertEqual(parameters["actualIsotropicMethod"], "geometric")
        self.assertEqual(parameters["nativeClass"], "SphereDistribution")
        self.assertAlmostEqual(float(parameters["nativeRadiusUm"]), 0.3, places=9)
        self.assertAlmostEqual(float(parameters["thicknessUm"]), 0.3, places=9)
        self.assertAlmostEqual(float(parameters["cells"]), 1.5, places=6)
        self.assertEqual(list(parameters["maskMaterials"]), [])
        self.assertTrue(parameters["nativeDurationIgnored"])
        self.assertTrue(parameters["reason"])
        self.assert_every_step_has_a_snapshot(run_id, info)
        final = self.snapshot(run_id, "step_iso_deposit")
        self.assertIn(30, {int(item) for item in final["cellData"]["materialIds"]})
        self.assertTrue(all(math.isfinite(float(value)) for point in final["points"]
                            for value in point))

    async def test_2d_auto_falls_back_to_level_set_for_unsafe_cases(self):
        # Sub-grid thickness cannot be represented by one geometric radius.
        value = project(2)
        value["steps"] = [iso_step("deposit", method="auto", rate=0.05, duration=1.0)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["requestedIsotropicMethod"], "auto")
        self.assertEqual(parameters["actualIsotropicMethod"], "levelSet")
        self.assertEqual(parameters["nativeClass"], "IsotropicProcess")
        self.assertAlmostEqual(float(parameters["timeStepRatio"]), 0.25, places=9)
        self.assert_every_step_has_a_snapshot(run_id, info)

        # A material table never takes the geometric path, even when the
        # thickness itself would be large enough.
        value = project(2, "trench")
        value["steps"] = [iso_step("etch", method="auto", rateMode="material",
                                   rateMagnitude=0.0, materialRates={"Si": 0.2},
                                   duration=1.0)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["actualIsotropicMethod"], "levelSet")
        self.assert_every_step_has_a_snapshot(run_id, info)

    async def test_3d_auto_uses_level_set_and_explicit_geometric_is_allowed(self):
        value = project(3)
        value["steps"] = [iso_step("deposit", method="auto", rate=0.3, duration=1.0)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["requestedIsotropicMethod"], "auto")
        self.assertEqual(parameters["actualIsotropicMethod"], "levelSet")
        self.assertEqual(parameters["nativeClass"], "IsotropicProcess")
        self.assertAlmostEqual(float(parameters["timeStepRatio"]), 0.25, places=9)
        self.assert_every_step_has_a_snapshot(run_id, info)

        # Explicit 3D geometric is a documented override.  It records why the
        # native duration and requested time-step ratio are not used.
        value = project(3)
        value["steps"] = [iso_step("etch", method="geometric", ratio=0.1,
                                   rate=0.3, duration=1.0)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["requestedIsotropicMethod"], "geometric")
        self.assertEqual(parameters["actualIsotropicMethod"], "geometric")
        self.assertEqual(parameters["nativeClass"], "SphereDistribution")
        self.assertAlmostEqual(float(parameters["nativeRadiusUm"]), -0.3, places=9)
        self.assertTrue(parameters["nativeDurationIgnored"])
        self.assertIn("ignor", json.dumps(parameters, ensure_ascii=False).lower())
        self.assert_every_step_has_a_snapshot(run_id, info)

    async def test_explicit_level_set_applies_the_requested_time_step_ratio(self):
        value = project(2)
        value["steps"] = [iso_step("etch", method="levelSet", ratio=0.1,
                                   rate=0.25, duration=0.4)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["requestedIsotropicMethod"], "levelSet")
        self.assertEqual(parameters["actualIsotropicMethod"], "levelSet")
        self.assertAlmostEqual(float(parameters["timeStepRatio"]), 0.1, places=9)
        self.assert_every_step_has_a_snapshot(run_id, info)
        final = self.snapshot(run_id, "step_iso_etch")
        self.assertTrue(all(math.isfinite(float(value)) for point in final["points"]
                            for value in point))

    async def test_deposit_material_rate_moves_only_the_listed_material(self):
        value = project(2, "trench")
        value["steps"] = [iso_step("deposit", rateMode="material", rateMagnitude=0.0,
                                   materialRates={"Si": 0.2}, duration=0.25)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["rateMode"], "material")
        self.assertEqual(float(parameters["actualDefaultRate"]), 0.0)
        self.assertAlmostEqual(parameters["actualMaterialRatesByName"]["Si"], 0.2, places=9)
        self.assertEqual({row["name"] for row in parameters["nativeRateTable"]}, {"Si"})
        self.assert_every_step_has_a_snapshot(run_id, info)

        initial = self.snapshot(run_id, "initial")
        final = self.snapshot(run_id, "step_iso_deposit")
        axis = vertical_axis(2)
        # The rate table is keyed by the material whose exposed surface may
        # move, so the listed Si floor is where the new SiO2 film grows; the
        # Si substrate interface itself and the unlisted mask stay in place.
        floor_before = exposed_floor(initial, 10, axis, False)
        floor_after = exposed_floor(final, 30, axis, False)
        mask_before = material_top(initial, 0, axis)
        mask_after = material_top(final, 0, axis)
        self.assertIsNotNone(floor_before)
        self.assertIsNotNone(floor_after)
        self.assertIsNotNone(mask_before)
        self.assertGreater(floor_after, floor_before + 0.01,
                           "the listed Si rate did not grow the deposited film on the Si floor")
        self.assertLessEqual(material_top(final, 30, axis), 1e-6,
                             "the film grew outside the exposed Si floor")
        self.assertAlmostEqual(mask_after, mask_before, places=6,
                               msg="default rate 0 must protect the unlisted Mask material")

    async def test_etch_material_rates_still_work(self):
        value = project(2, "trench")
        value["steps"] = [iso_step("etch", rateMode="material", rateMagnitude=0.0,
                                   materialRates={"Si": 0.2}, duration=0.3)]
        run_id, info = await self.execute(value)
        parameters = self.last_parameters(info)
        self.assertEqual(parameters["rateMode"], "material")
        self.assertAlmostEqual(parameters["actualMaterialRatesByName"]["Si"], -0.2, places=9)
        self.assert_every_step_has_a_snapshot(run_id, info)

        initial = self.snapshot(run_id, "initial")
        final = self.snapshot(run_id, "step_iso_etch")
        axis = vertical_axis(2)
        self.assertLess(exposed_floor(final, 10, axis, False),
                        exposed_floor(initial, 10, axis, False) - 0.01)
        self.assertAlmostEqual(material_top(final, 0, axis),
                               material_top(initial, 0, axis), places=6)

    async def test_api_rejects_unknown_material_with_422(self):
        value = project(2)
        value["steps"] = [iso_step("deposit", rateMode="material", rateMagnitude=0.0,
                                   materialRates={"NotAMaterial": 0.1})]
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("NotAMaterial", response[2].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
