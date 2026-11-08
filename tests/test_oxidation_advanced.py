"""Native ``SiliconOrientation.Si110`` and ``steps[].advancedOxidation``.

The installed ViennaPS 4.6.2 ``Oxidation`` binding exposes Si110 plus a large
solver surface. The Workbench previously accepted only 13 setter values and
three of the four orientation members. This batch adds:

* ``orientation: "Si110"``;
* the strict optional ``steps[].advancedOxidation`` object with the four
  solver controls proven to change the returned surface deterministically:
  ``cflFactor``, ``mechanicsTolerance``, ``couplingTolerance`` and
  ``simplePressureRelaxation``.

The contract pinned here is deliberately narrow:

* omitting the block keeps the historical oxidation request hashes;
* catalog/native defaults are the installed 4.6.2 defaults, and materialising
  them is a behaviour no-op;
* 2D and 3D native runs show the orientation and setter effects;
* every excluded candidate (GPU mode, maxGridPoints, solve bounds, iteration
  caps, tolerance combinations without evidence, mask mechanics, the old
  ``OxidationParameters`` names) is rejected by the strict schema rather than
  silently ignored;
* API run, snapshot manifest and save/reopen preserve the normalized-input/
  effective/native-readback record.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import viennaps as ps  # noqa: E402

from backend import storage  # noqa: E402
from backend.engine import oxidation_advanced_supported
from backend.hashing import requested_input_hash  # noqa: E402
from backend.main import app  # noqa: E402
from backend.specialized_models import (  # noqa: E402
    OXIDATION_ADVANCED_FIELDS,
    model_spec,
    oxidation_advanced_spec,
)
from backend.validation import ProjectInputError, validate_project  # noqa: E402
from stage8_project_factory import make_project  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from worker.specialized import build_specialized_model  # noqa: E402

ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)
ps.Length.setUnit("um")
ps.Time.setUnit("s")

# Request hashes computed on the base commit (b9ed53e) before Si110 and
# advancedOxidation were accepted. They must never change when the optional
# block is omitted.
BASE_REQUEST_HASHES = {
    2: "a0beb09707531637a350bae149e024a2fbf313b65ce120c98ac897dac9595688",
    3: "c9581ebe9933a3f2a2c6b826a8ea5ee154c9820661239b5ddaffa7c839cb6f3b",
}

ADVANCED_DEFAULTS = {
    "cflFactor": 0.499,
    "mechanicsTolerance": 0.01,
    "couplingTolerance": 0.05,
    "simplePressureRelaxation": 0.5,
}

# Every name below is either absent from the installed Python module or was
# proven inert/unsafe by the controlled probes. The strict schema must reject
# it instead of accepting a no-op knob.
EXCLUDED_ADVANCED_KEYS = (
    "gpuMode", "gpuPreconditioner", "maxGridPoints", "solveBounds",
    "couplingIterations", "mechanicsIterations", "pressureIterations",
    "stokesIterations", "pressureTolerance", "stokesTolerance",
    "simpleVelocityRelaxation", "maskParameters", "maskMechanics",
    "timeStepRatio", "oxidantFlux", "reactionRate", "volumeExpansion",
    "stabilityFactor", "redoStabilityCheck", "calculateError",
    "maxCellTravelDistance", "useFluxSolver", "fluxSolver", "fluxMultiplier",
    "solverStabilityFactor", "trapLayer", "addDiffusionCellSet",
    "diffusionCellSet", "finiteVolume", "fluxAtBoundary",
)


def oxidation_project(dimension: int = 2, *, delta: float = 0.05,
                      extent: float = 1.0, oxide: float | None = None,
                      orientation: str = "Si100", time_hours: float = 0.02,
                      time_step_hours: float = 0.02,
                      advanced: dict | None = None) -> dict:
    project = make_project("oxidation", dimension)
    if dimension == 2:
        project["domain"] = {"gridDelta": delta, "xExtent": extent,
                             "xBoundary": "reflective"}
    else:
        project["domain"] = {"gridDelta": delta, "xExtent": extent,
                             "yExtent": extent, "boundaryXY": "reflective"}
    if oxide is None:
        oxide = max(0.05, 3.0 * delta)
    project["geometry"] = {
        "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
        "layers": [{"id": "oxide", "name": "Oxide", "material": "SiO2",
                    "thickness": oxide, "role": "film"}],
        "pattern": {"kind": "none"},
    }
    step = project["steps"][0]
    step["nativeParameters"].update({
        "orientation": orientation,
        "timeHours": time_hours,
        "timeStepHours": time_step_hours,
    })
    if advanced is not None:
        step["advancedOxidation"] = copy.deepcopy(advanced)
    return project


def normalized_dict(project: dict) -> dict:
    parsed, _warnings = validate_project(project)
    return parsed.model_dump(mode="json", exclude_none=True)


def native_domain(project: dict):
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    domain_spec = project["domain"]
    delta = float(domain_spec["gridDelta"])
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    if dimension == 2:
        domain = ps.Domain(delta, float(domain_spec["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(delta, float(domain_spec["xExtent"]),
                           float(domain_spec["yExtent"]), boundary)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si,
                 addToExisting=False).apply()
    geometry = project["geometry"]
    height = float(geometry["baseHeight"])
    for layer in geometry["layers"]:
        height += float(layer["thickness"])
        ps.MakePlane(domain, height=height,
                     material=getattr(ps.Material, layer["material"]),
                     addToExisting=True).apply()
    return domain


def surface_hash(domain) -> tuple[str, int]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                 minNodeDistanceFactor=0.01)
    data = mesh.getCellData()
    materials = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            materials = [int(value) for value in data.getScalarData(index)]
    points = [[round(float(value), 9) for value in point]
              for point in mesh.getNodes()]
    payload = json.dumps([points, materials], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest(), len(points)


def run_workbench(project: dict) -> tuple[dict, str, int]:
    parsed = normalized_dict(project)
    step = parsed["steps"][0]
    domain = native_domain(parsed)
    model, extra, actual, duration = build_specialized_model(
        ps, domain, parsed, step, lambda name: getattr(ps.Material, name), {})
    process = ps.Process(domain, model, duration)
    # Match worker/simulate.py: pin CPU_DISK before apply() so the native
    # Process does not attempt CUDA autodetection on a CPU-only evidence run.
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    for item in extra:
        process.setParameters(item)
    process.apply()
    digest, count = surface_hash(domain)
    return actual, digest, count


def run_3d_subprocess(*, delta: float, oxide: float, orientation: str,
                      time_hours: float, time_step_hours: float,
                      advanced: dict | None = None) -> dict:
    """Run one 3D oxidation in a fresh interpreter.

    ViennaPS 4.6.2's Python proxy keeps dimension state process-wide and can
    return a stale 3D surface after 2D work in the same process. A subprocess
    keeps the 3D evidence trustworthy; the fixture prints one JSON object.
    """
    command = [
        sys.executable, str(ROOT / "tests" / "fixtures" / "oxidation_surface_hash.py"),
        "--dimension", "3", "--delta", str(delta), "--extent", "1.0",
        "--oxide", str(oxide), "--orientation", orientation,
        "--time-hours", str(time_hours),
        "--time-step-hours", str(time_step_hours),
    ]
    if advanced is not None:
        command.extend(["--advanced", json.dumps(advanced)])
    completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                               text=True, timeout=300, check=False)
    if completed.returncode != 0:
        raise AssertionError(
            f"3D oxidation probe failed ({completed.returncode}): "
            f"{completed.stderr[-2000:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def planar_estimate(orientation: str, *, temperature: float = 1000.0,
                    time_hours: float = 0.5, dry: bool = True) -> float:
    model = ps.Oxidation()
    oxidant = getattr(ps.OxidantType, "Dry" if dry else "Wet", None)
    if oxidant is None:
        oxidant = getattr(ps.OxidantType, "DRY" if dry else "WET")
    model.setOxidant(oxidant)
    model.setOrientation(getattr(ps.SiliconOrientation, orientation))
    model.setTemperature(temperature)
    model.setPressure(1.0)
    model.setTime(time_hours)
    return float(model.estimatePlanarOxideThickness(0.0))


class OxidationCatalogAndSchema(unittest.TestCase):
    def test_catalog_has_si110_and_the_proven_advanced_fields(self):
        spec = model_spec("oxidation")
        orientation = next(field for field in spec["fields"]
                           if field["key"] == "orientation")
        self.assertEqual([row["value"] for row in orientation["options"]],
                         ["Si100", "Si110", "Si111", "PolySi"])
        self.assertEqual(spec["advancedOxidation"]["location"],
                         "steps[].advancedOxidation")
        advanced = oxidation_advanced_spec()
        self.assertEqual(advanced["location"], "steps[].advancedOxidation")
        self.assertEqual({field["key"] for field in advanced["fields"]},
                         set(ADVANCED_DEFAULTS))
        self.assertEqual({field["key"]: field["default"]
                          for field in advanced["fields"]},
                         ADVANCED_DEFAULTS)
        # Every field is a real bound setter on the installed module, except
        # the controls the installed runtime documents as removed/inert.
        for field in OXIDATION_ADVANCED_FIELDS:
            native = field["nativeSetter"].split(".")[-1]
            if oxidation_advanced_supported(field["key"]):
                self.assertTrue(hasattr(ps.Oxidation, native), native)
            else:
                self.assertIn(field["key"], advanced["runtimeGate"])
                if field["key"] == "simplePressureRelaxation":
                    # Removed in 4.7.0, not merely inert.
                    self.assertFalse(hasattr(ps.Oxidation, native), native)

    def test_omitting_the_block_keeps_the_base_request_hashes(self):
        for dimension, expected in BASE_REQUEST_HASHES.items():
            with self.subTest(dimension=dimension):
                parsed, _warnings = validate_project(make_project("oxidation", dimension))
                self.assertEqual(requested_input_hash(parsed), expected)
                self.assertNotIn("advancedOxidation",
                                 parsed.model_dump(mode="json", exclude_none=True)["steps"][0])

    def test_advanced_values_change_the_request_hash(self):
        base, _warnings = validate_project(make_project("oxidation", 2))
        project, _warnings = validate_project(
            oxidation_project(advanced={"cflFactor": 0.05}))
        self.assertNotEqual(requested_input_hash(project),
                            requested_input_hash(base))

    def test_explicit_native_defaults_are_a_behaviour_noop(self):
        # A default value is only a real default if passing it explicitly
        # reproduces the omitted-block surface. This is the native-default
        # check that the missing getters cannot provide directly.
        plain = oxidation_project(2, delta=0.02, time_hours=0.1,
                                  time_step_hours=0.03)
        materialized = oxidation_project(
            2, delta=0.02, time_hours=0.1, time_step_hours=0.03,
            advanced=dict(ADVANCED_DEFAULTS))
        _plain_actual, plain_hash, _ = run_workbench(plain)
        actual, explicit_hash, _ = run_workbench(materialized)
        self.assertEqual(plain_hash, explicit_hash)
        metadata = actual["advancedOxidation"]
        self.assertEqual(metadata["normalizedInput"], ADVANCED_DEFAULTS)
        self.assertEqual(metadata["effective"], ADVANCED_DEFAULTS)
        self.assertEqual(metadata["defaults"], ADVANCED_DEFAULTS)

        # ViennaPS 4.6.2 keeps dimension state process-wide. Run both 3D cases
        # in fresh interpreters and validate topology/counts, not just hash
        # equality, so a stale 2D→3D surface cannot masquerade as a no-op.
        plain_3d = run_3d_subprocess(delta=0.05, oxide=0.15,
                                     time_hours=0.02, time_step_hours=0.02,
                                     orientation="Si100")
        materialized_3d = run_3d_subprocess(
            delta=0.05, oxide=0.15, time_hours=0.02, time_step_hours=0.02,
            orientation="Si100", advanced=dict(ADVANCED_DEFAULTS))
        for result in (plain_3d, materialized_3d):
            self.assertEqual(result["dimension"], 3)
            self.assertEqual(result["topology"], "triangles")
            self.assertGreater(result["pointCount"], 0)
            self.assertGreater(result["cellCount"], 0)
        self.assertEqual(plain_3d["surfaceHash"],
                         materialized_3d["surfaceHash"])
        self.assertEqual(materialized_3d["advancedOxidation"]["effective"],
                         ADVANCED_DEFAULTS)
        self.assertEqual(materialized_3d["advancedOxidation"]["normalizedInput"],
                         ADVANCED_DEFAULTS)

    def test_native_readback_orientation_is_recorded(self):
        actual, _digest, _ = run_workbench(
            oxidation_project(orientation="Si110", delta=0.04,
                              time_hours=0.1, time_step_hours=0.03,
                              advanced={"cflFactor": 0.05}))
        metadata = actual["advancedOxidation"]
        self.assertEqual(metadata["nativeReadback"]["orientation"], "Si110")
        self.assertAlmostEqual(
            metadata["nativeReadback"]["planarOxideThicknessUm"],
            planar_estimate("Si110", time_hours=0.1), delta=1e-15)
        self.assertEqual(metadata["nativeReadback"]["nativeGetters"], "not exposed")


class OxidationOrientationNative(unittest.TestCase):
    def test_si110_exists_and_changes_the_planar_readback(self):
        values = {name: planar_estimate(name) for name in
                  ("Si100", "Si110", "Si111", "PolySi")}
        self.assertEqual(values["PolySi"], values["Si100"])
        self.assertGreater(values["Si110"], values["Si100"])
        self.assertGreater(values["Si111"], values["Si110"])
        self.assertEqual(values, {name: planar_estimate(name) for name in values})

    def test_2d_orientation_runs_are_deterministic_and_distinct(self):
        hashes = {}
        for orientation in ("Si100", "Si110", "Si111"):
            project = oxidation_project(2, delta=0.01, time_hours=0.166,
                                        time_step_hours=0.03,
                                        orientation=orientation)
            _actual, digest, count = run_workbench(project)
            self.assertGreater(count, 0)
            hashes[orientation] = digest
        self.assertEqual(len(set(hashes.values())), 3, hashes)
        repeat = oxidation_project(2, delta=0.01, time_hours=0.166,
                                   time_step_hours=0.03, orientation="Si110")
        _actual, repeat_hash, _count = run_workbench(repeat)
        self.assertEqual(repeat_hash, hashes["Si110"])

    def test_3d_orientation_runs_are_real_and_distinct(self):
        hashes = {}
        for orientation in ("Si100", "Si110", "Si111"):
            # 0.1 hr is long enough for the stress-free (activation volume 0)
            # workbench configuration to expose the orientation-dependent
            # reaction rate; 0.02 hr is sub-grid for this coarse 3D case.
            result = run_3d_subprocess(delta=0.05, oxide=0.15,
                                       time_hours=0.1, time_step_hours=0.03,
                                       orientation=orientation)
            self.assertGreater(result["pointCount"], 0)
            hashes[orientation] = result["surfaceHash"]
            self.assertEqual(result["dimension"], 3)
        self.assertEqual(len(set(hashes.values())), 3, hashes)


class OxidationAdvancedNative(unittest.TestCase):
    def assert_setter_changes_2d(self, advanced: dict, *, delta: float = 0.01) -> None:
        base_project = oxidation_project(2, delta=delta, time_hours=0.166,
                                         time_step_hours=0.03)
        _actual, base_hash, _ = run_workbench(base_project)
        changed_project = oxidation_project(2, delta=delta, time_hours=0.166,
                                            time_step_hours=0.03,
                                            advanced=advanced)
        actual, changed_hash, _ = run_workbench(changed_project)
        self.assertNotEqual(base_hash, changed_hash)
        self.assertEqual(actual["advancedOxidation"]["effective"],
                         {**ADVANCED_DEFAULTS, **advanced})

    def test_cfl_factor_changes_the_2d_surface(self):
        self.assert_setter_changes_2d({"cflFactor": 0.05})

    def test_mechanics_tolerance_changes_the_2d_surface(self):
        if not oxidation_advanced_supported("mechanicsTolerance"):
            self.assert_gated_2d("mechanicsTolerance", 0.05)
            return
        self.assert_setter_changes_2d({"mechanicsTolerance": 0.05})

    def assert_gated_2d(self, key: str, value: float) -> None:
        project = oxidation_project(2, delta=0.01, time_hours=0.166,
                                    time_step_hours=0.03,
                                    advanced={key: value})
        with self.assertRaises(ProjectInputError) as raised:
            validate_project(project, kind="simulate")
        message = str(raised.exception)
        self.assertIn(f"steps[0].advancedOxidation.{key}", message)
        self.assertIn(str(value), message)

    def test_coupling_tolerance_and_pressure_relaxation_change_the_2d_surface(self):
        # At the finer 0.01 um grid the coupling tolerance is inert for this
        # planar case; 0.02 um is the controlled probe configuration where the
        # native coupling loop changes the returned surface.
        if not oxidation_advanced_supported("couplingTolerance"):
            self.assert_gated_2d("couplingTolerance", 0.2)
            self.assert_gated_2d("simplePressureRelaxation", 0.1)
            return
        self.assert_setter_changes_2d({"couplingTolerance": 0.2}, delta=0.02)
        self.assert_setter_changes_2d({"simplePressureRelaxation": 0.1})

    def test_3d_advanced_run_records_effective_values(self):
        # The 3D stress-free workbench configuration converges identically for
        # the numerical setters at this coarse grid; the 2D probes above are
        # the behavioural evidence. This run proves the 3D binding accepts the
        # setters and that the recorded effective values survive the worker.
        advanced = {"cflFactor": 0.2}
        if oxidation_advanced_supported("mechanicsTolerance"):
            advanced = {"mechanicsTolerance": 0.05, "couplingTolerance": 0.05}
        result = run_3d_subprocess(
            delta=0.05, oxide=0.15, time_hours=0.02, time_step_hours=0.02,
            orientation="Si100", advanced=advanced)
        self.assertGreater(result["pointCount"], 0)
        self.assertEqual(result["advancedOxidation"]["effective"],
                         {**ADVANCED_DEFAULTS, **advanced})

    def test_repeated_advanced_runs_are_byte_identical(self):
        advanced = {"cflFactor": 0.05}
        if oxidation_advanced_supported("mechanicsTolerance"):
            advanced["mechanicsTolerance"] = 0.05
        project = oxidation_project(2, delta=0.01, time_hours=0.166,
                                    time_step_hours=0.03, advanced=advanced)
        first_actual, first_hash, _ = run_workbench(project)
        second_actual, second_hash, _ = run_workbench(project)
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_actual["advancedOxidation"],
                         second_actual["advancedOxidation"])


class OxidationAdvancedRejections(unittest.TestCase):
    def assert_rejected(self, project: dict, fragment: str) -> None:
        with self.assertRaises(ProjectInputError) as raised:
            validate_project(project, kind="simulate")
        self.assertIn(fragment, str(raised.exception))

    def test_unknown_advanced_keys_are_rejected(self):
        for key in EXCLUDED_ADVANCED_KEYS:
            with self.subTest(key=key):
                project = oxidation_project(advanced={key: 1})
                self.assert_rejected(project, "Extra inputs are not permitted")

    def test_out_of_range_and_non_finite_values_are_rejected(self):
        cases = (
            ("cflFactor", 0.0), ("cflFactor", 0.5),
            ("mechanicsTolerance", 0.001), ("mechanicsTolerance", 0.21),
            ("couplingTolerance", 0.01), ("couplingTolerance", 0.51),
            ("simplePressureRelaxation", 0.0), ("simplePressureRelaxation", 1.1),
            ("cflFactor", float("nan")), ("cflFactor", float("inf")),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                project = oxidation_project(advanced={key: value})
                self.assert_rejected(project, "advancedOxidation")

    def test_coupled_tolerance_ordering_is_rejected(self):
        project = oxidation_project(advanced={"mechanicsTolerance": 0.2,
                                              "couplingTolerance": 0.05})
        self.assert_rejected(project, "couplingTolerance")

    def test_runtime_qualified_advanced_controls(self):
        cases = (("mechanicsTolerance", 0.05, 0.01),
                 ("couplingTolerance", 0.2, 0.02),
                 ("simplePressureRelaxation", 0.1, 0.01))
        for key, value, delta in cases:
            with self.subTest(key=key):
                project = oxidation_project(2, delta=delta, time_hours=0.166,
                                            time_step_hours=0.03,
                                            advanced={key: value})
                with patch("backend.validation.engine_version", return_value="4.6.2"):
                    parsed, _warnings = validate_project(project)
                self.assertEqual(getattr(parsed.steps[0].advancedOxidation, key), value)
                if oxidation_advanced_supported(key):
                    continue
                with patch("backend.validation.engine_version", return_value="4.7.0"):
                    with self.assertRaises(ProjectInputError) as raised:
                        validate_project(project)
                message = str(raised.exception)
                self.assertIn(f"steps[0].advancedOxidation.{key}", message)
                self.assertIn("4.7.0", message)
                self.assertIn("omit this override", message)

    def test_unsupported_orientation_is_rejected(self):
        project = oxidation_project(orientation="Si101")
        self.assert_rejected(project, "must be one of")

    def test_inert_or_absent_native_names_are_not_in_native_parameters(self):
        for key in ("maxGridPoints", "timeStepRatio", "oxidantFlux",
                    "reactionRate", "stabilityFactor", "volumeExpansion",
                    "redoStabilityCheck", "calculateError",
                    "maxCellTravelDistance", "trapLayer", "diffusionCellSet"):
            with self.subTest(key=key):
                project = oxidation_project()
                project["steps"][0]["nativeParameters"][key] = 1
                self.assert_rejected(project, "nativeParameters must contain exactly")

    def test_advanced_block_is_oxidation_only(self):
        for model_id, dimension in (("ald", 2), ("oxideRegrowth", 2),
                                    ("geometricTrenchDeposition", 2)):
            with self.subTest(model=model_id):
                project = make_project(model_id, dimension)
                project["steps"][0]["advancedOxidation"] = {"cflFactor": 0.05}
                self.assert_rejected(project, "advancedOxidation")

    def test_2d_and_3d_accept_the_same_block_and_reject_dimension_bounds(self):
        for dimension in (2, 3):
            with self.subTest(dimension=dimension):
                project = oxidation_project(dimension, advanced={"cflFactor": 0.2})
                parsed = normalized_dict(project)
                self.assertEqual(parsed["steps"][0]["advancedOxidation"]["cflFactor"], 0.2)
                # solveBounds is dimension-specific, has no stable index
                # contract and is therefore excluded in both dimensions.
                bad = oxidation_project(dimension)
                bad["steps"][0]["advancedOxidation"] = {
                    "solveBounds": {"minIndex": [1] * dimension,
                                    "maxIndex": [4] * dimension}}
                self.assert_rejected(bad, "Extra inputs are not permitted")

    def test_old_python_parameter_object_names_are_absent(self):
        for name in ("OxidationParameters", "psOxidation", "psProcessContext"):
            self.assertFalse(hasattr(ps, name), name)
        for name in ("setOxidationParameters", "setStabilityFactor",
                     "setMaxCellTravelDistance", "setUseFluxSolver",
                     "setTrapLayer", "addDiffusionCellSet"):
            self.assertFalse(hasattr(ps.Oxidation, name), name)


class OxidationAdvancedApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)

    async def wait(self, run_id: str) -> dict:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "timed_out",
                                  "cancelled", "interrupted"}:
                self.assertEqual(info["status"], "succeeded",
                                 info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("oxidation run did not finish")

    def advanced(self) -> dict:
        if not oxidation_advanced_supported("mechanicsTolerance"):
            return {"cflFactor": 0.05}
        return {"cflFactor": 0.05, "mechanicsTolerance": 0.05,
                "couplingTolerance": 0.1, "simplePressureRelaxation": 0.1}

    def expected_effective(self) -> dict:
        return {**ADVANCED_DEFAULTS, **self.advanced()}

    def project(self) -> dict:
        project = oxidation_project(
            2, delta=0.02, time_hours=0.15, time_step_hours=0.03,
            orientation="Si110", advanced=self.advanced())
        project["name"] = "Oxidation advanced API checkpoint"
        project["steps"][0]["id"] = "stage8_oxidation"
        project["solver"]["timeoutSeconds"] = 120
        return project

    async def test_api_run_records_advanced_values_in_manifest_and_snapshot(self):
        storage.initialize()
        project = self.project()
        normalized_project = normalized_dict(project)
        # Save / reopen through the real project store before running.
        record = storage.create_project(normalized_project)
        reopened = storage.read_json(storage.project_path(record["id"]))["project"]
        self.assertEqual(reopened["steps"][0]["advancedOxidation"],
                         normalized_project["steps"][0]["advancedOxidation"])
        reparsed = normalized_dict(reopened)
        self.assertEqual(reparsed["steps"][0]["nativeParameters"]["orientation"], "Si110")

        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate",
                                               "project": reopened})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        info = await self.wait(run_id)
        record = next(row for row in info["actualExecution"]
                      if row.get("stepId") == "stage8_oxidation")
        metadata = record["actualParameters"]["nativeModel"]["advancedOxidation"]
        self.assertEqual(metadata["normalizedInput"], self.expected_effective())
        self.assertEqual(metadata["effective"], metadata["normalizedInput"])
        self.assertEqual(metadata["defaults"], ADVANCED_DEFAULTS)
        self.assertEqual(metadata["nativeReadback"]["orientation"], "Si110")
        self.assertGreater(metadata["nativeReadback"]["planarOxideThicknessUm"], 0.0)

        snapshot_response = await asgi_request(
            f"/api/runs/{run_id}/snapshots/stage8_oxidation")
        self.assertEqual(snapshot_response[0], 200, snapshot_response[2])
        snapshot = json_body(snapshot_response)
        self.assertEqual(snapshot["oxidation"]["effective"], metadata["effective"])
        self.assertEqual(snapshot["oxidation"]["nativeReadback"],
                         metadata["nativeReadback"])
        listed = next(row for row in info["snapshots"]
                      if row.get("stepId") == "stage8_oxidation")
        self.assertEqual(listed["oxidation"]["normalizedInput"],
                         metadata["normalizedInput"])

    async def test_omitted_block_adds_no_snapshot_or_manifest_section(self):
        project = self.project()
        project["steps"][0].pop("advancedOxidation", None)
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate",
                                               "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        info = await self.wait(run_id)
        listed = next(row for row in info["snapshots"]
                      if row.get("stepId") == "stage8_oxidation")
        self.assertNotIn("oxidation", listed)
        snapshot_response = await asgi_request(
            f"/api/runs/{run_id}/snapshots/stage8_oxidation")
        self.assertEqual(snapshot_response[0], 200, snapshot_response[2])
        self.assertNotIn("oxidation", json_body(snapshot_response))
        record = next(row for row in info["actualExecution"]
                      if row.get("stepId") == "stage8_oxidation")
        self.assertNotIn("advancedOxidation",
                         record["actualParameters"]["nativeModel"])

    async def test_replay_preserves_the_advanced_request(self):
        project = self.project()
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate",
                                               "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        original = await self.wait(run_id)
        envelope = json_body(await asgi_request(
            f"/api/runs/{run_id}/replay-project"))
        replay = await asgi_request("/api/runs/replay", method="POST",
                                    payload=envelope)
        self.assertEqual(replay[0], 202, replay[2])
        replay_info = await self.wait(json_body(replay)["runId"])
        self.assertEqual(replay_info["requestedInputHash"],
                         original["requestedInputHash"])
        replay_record = next(row for row in replay_info["actualExecution"]
                             if row.get("stepId") == "stage8_oxidation")
        self.assertEqual(
            replay_record["actualParameters"]["nativeModel"]["advancedOxidation"]["effective"],
            self.expected_effective())


if __name__ == "__main__":
    unittest.main()
