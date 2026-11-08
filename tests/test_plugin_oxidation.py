"""Tests for the native v2 silicon thermal-oxidation plugin.

The tests never call :func:`worker.process_models_v2.run_v2_step` or
``load_registry``: adding a plugin makes the checked-in ``plugins.lock`` stale,
and this suite must not depend on a lock that the main workspace owns. Instead
every test builds a manual :class:`~process_models.v2.registry.ProcessRegistry`,
registers ``plugins.oxidation`` into it, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` whose handle is the test
module ``viennaps``. The lock is therefore never consulted.

Each test builds one small 2D/3D level-set domain, runs the
``oxidation``/``native`` registration, and compares the result with a direct
``ps.Oxidation`` + ``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`. Oxidation has no flux model
and no ray tracing, so the two runs are bit-for-bit comparable without the
seeded-module proxy the plasma plugin tests need. No server or worker process
is involved; the v2 state store writes into ``var/test-plugin-oxidation/``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import model_spec
from plugins.oxidation import (
    OxidationNativeVariant,
    OxidationParams,
    OxidationProcessSpec,
    parse_step,
    register,
)
from plugins.oxidation.params import MATERIAL_FIELDS, NUMERIC_FIELDS
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import configure_advection, material_handle
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-oxidation"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
ENGINE_FINGERPRINT = "test-oxidation"

#: ``backend.specialized_models`` defaults for the ``oxidation`` catalog row.
CATALOG_DEFAULTS: dict[str, Any] = {
    "oxidant": "Dry",
    "orientation": "Si100",
    "temperatureC": 1000.0,
    "pressureAtm": 1.0,
    "timeHours": 0.001,
    "timeStepHours": 0.0001,
    "transferCoefficientUmHr": 0.1,
    "initialOxideThicknessUm": 0.0,
    "reactionActivationVolumeM3": 0.0,
    "diffusionActivationVolumeM3": 0.0,
    "maskMaterial": "",
    "siliconMaterial": "Si",
    "oxideMaterial": "SiO2",
}


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
        "random": {"mode": "fixed", "seed": 0},
        "advection": {
            "spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
            "temporalScheme": "FORWARD_EULER",
            "adaptiveTimeStepping": False,
            "adaptiveTimeStepSubdivisions": 20,
            "calculateIntermediateVelocities": False,
            "checkDissipation": True,
            "dissipationAlpha": 1.0,
            "ignoreVoids": False,
            "velocityOutput": False,
        },
    }


def oxidation_step(
    *,
    native_parameters: dict[str, Any] | None = None,
    **step_fields: Any,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    step: dict[str, Any] = {
        "id": "step_oxidation",
        "name": "Thermal oxidation",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "oxidation",
        "operation": "native",
        "nativeParameters": parameters,
    }
    step.update(step_fields)
    return step


def oxidation_project(
    dimension: int = 2,
    *,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = X_EXTENT
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"oxidation plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [oxidation_step(native_parameters=native_parameters)],
        "solver": _solver(),
    }


def plane_domain(dimension: int = 2) -> Any:
    ps.setDimension(dimension)
    if dimension == 2:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=X_EXTENT,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    else:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=X_EXTENT,
            yExtent=X_EXTENT,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


def _oxidant(ps_module: Any, name: str) -> Any:
    oxidant = getattr(ps_module.OxidantType, name, None)
    if oxidant is None:
        oxidant = getattr(ps_module.OxidantType, name.upper(), None)
    if oxidant is None:
        raise AssertionError(f"installed engine has no OxidantType {name!r}")
    return oxidant


def _reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Build the oxidation path directly, with no worker code."""
    values = project["steps"][0]["nativeParameters"]
    model = ps_module.Oxidation()
    model.setOxidant(_oxidant(ps_module, values["oxidant"]))
    model.setOrientation(getattr(ps_module.SiliconOrientation, values["orientation"]))
    model.setTemperature(float(values["temperatureC"]))
    model.setPressure(float(values["pressureAtm"]))
    model.setTime(float(values["timeHours"]))
    model.setTimeStep(float(values["timeStepHours"]))
    model.setTransferCoefficient(float(values["transferCoefficientUmHr"]))
    model.setInitialOxideThickness(float(values["initialOxideThicknessUm"]))
    model.setReactionActivationVolume(float(values["reactionActivationVolumeM3"]))
    model.setDiffusionActivationVolume(float(values["diffusionActivationVolumeM3"]))
    model.setSiliconMaterial(material_handle(ps_module, values["siliconMaterial"]))
    model.setOxideMaterial(material_handle(ps_module, values["oxideMaterial"]))

    process = ps_module.Process(domain, model, float(values["timeHours"]) * 3600.0)
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    process.apply()
    return domain


def _cpu_provider(handle: Any, version: str = VIENNAPS_VERSION) -> Any:
    return StaticEngineProvider(
        [
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id="viennaps",
                    version=version,
                    kind="native",
                    backends=("cpu",),
                    kernels=("CPU_DISK",),
                ),
                handle=handle,
                fingerprint=ENGINE_FINGERPRINT,
            )
        ]
    )


class OxidationPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        try:
            TEST_ROOT.rmdir()
        except OSError:
            pass

    # -- identity / parameters --------------------------------------------

    def test_identity_matches_the_manifest_contract(self) -> None:
        manifest = tomllib.loads((ROOT / "plugins" / "oxidation" / "plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "oxidation")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.oxidation:register")

        self.assertEqual(OxidationProcessSpec.id, "oxidation")
        self.assertEqual(OxidationProcessSpec.version, "1.0.0")
        self.assertEqual(OxidationProcessSpec.params_model, OxidationParams)
        self.assertEqual(OxidationProcessSpec.dimensions, (2, 3))
        self.assertEqual(OxidationProcessSpec.operations, ("native",))
        self.assertEqual(OxidationProcessSpec.default_variant, "native")
        self.assertIs(OxidationProcessSpec.parse_step, parse_step)

        variant = OxidationNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertFalse(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertTrue(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        profiles = {(profile.backend, profile.kernel) for profile in variant.engine.profiles}
        self.assertEqual(profiles, {("cpu", "CPU_DISK")})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "oxidation")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("oxidation", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("oxidation"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("oxidation")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))

        params = OxidationParams()
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
        for name in NUMERIC_FIELDS:
            with self.subTest(field=name):
                model_field = OxidationParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertEqual(
            [option["value"] for option in fields["oxidant"]["options"]],
            [value for value in ("Dry", "Wet")],
        )
        self.assertEqual(
            [option["value"] for option in fields["orientation"]["options"]],
            ["Si100", "Si110", "Si111", "PolySi"],
        )
        self.assertEqual(set(MATERIAL_FIELDS), {"maskMaterial", "siliconMaterial", "oxideMaterial"})

        self.assertAlmostEqual(params.duration_seconds, 3.6, places=9)
        self.assertEqual(params.native_values(), CATALOG_DEFAULTS)

        invalid = (
            {"timeHours": 0.0},
            {"timeHours": -0.001},
            {"timeHours": 0.2},
            {"timeHours": float("nan")},
            {"timeStepHours": 0.0},
            {"timeStepHours": float("inf")},
            {"temperatureC": 799.0},
            {"temperatureC": 1201.0},
            {"pressureAtm": 0.0},
            {"transferCoefficientUmHr": -0.1},
            {"initialOxideThicknessUm": -1.0},
            {"reactionActivationVolumeM3": -1.0},
            {"diffusionActivationVolumeM3": 1_000_001.0},
            {"oxidant": "dry"},
            {"orientation": "si100"},
            {"siliconMaterial": ""},
            {"oxideMaterial": ""},
            {"unexpected": 1},
            {"timeHours": "0.001"},
            {"timeHours": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    OxidationParams(**overrides)

        with self.assertRaises(ValidationError):
            params.timeHours = 0.002  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = oxidation_project()
        step = project["steps"][0]
        self.assertNotIn("duration", step)

        params = parse_step(project, step)
        self.assertIsInstance(params, OxidationParams)
        assert params is not None
        self.assertEqual(params.native_values(), CATALOG_DEFAULTS)
        self.assertAlmostEqual(params.duration_seconds, 3.6, places=9)

        sparse = oxidation_project()
        sparse["steps"][0]["nativeParameters"] = {"timeHours": 0.002, "temperatureC": 900}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.timeHours, 0.002)
        self.assertEqual(params.temperatureC, 900.0)
        self.assertEqual(params.oxidant, "Dry")
        self.assertEqual(params.oxideMaterial, "SiO2")

        with_duration = oxidation_project()
        with_duration["steps"][0]["duration"] = 12.0
        params = parse_step(with_duration, with_duration["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        # The step duration is ignored: the native process duration comes from
        # timeHours, as in the worker.
        self.assertAlmostEqual(params.duration_seconds, 3.6, places=9)

        claimed = self.registry.resolve_step(project, step)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed[0].id, "oxidation")
        self.assertIsInstance(claimed[1], OxidationParams)

    def test_parse_step_rejects_masked_advanced_unknown_and_other_processes(self) -> None:
        project = oxidation_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        masked = mutated()
        masked["nativeParameters"]["maskMaterial"] = "Si3N4"
        advanced = mutated()
        advanced["advancedOxidation"] = {"cflFactor": 0.5}
        nested_advanced = mutated()
        nested_advanced["nativeParameters"]["advanced"] = {"cflFactor": 0.5}
        unknown = mutated()
        unknown["nativeParameters"]["unknownKey"] = 1
        other_model = mutated(nativeModel="sf6o2")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="etch")

        for label, candidate in (
            ("masked", masked),
            ("advanced", advanced),
            ("nested advanced", nested_advanced),
            ("unknown", unknown),
            ("sf6o2", other_model),
            ("isotropic", not_specialized),
            ("etch", other_operation),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        unmasked = mutated()
        unmasked["nativeParameters"]["maskMaterial"] = ""
        self.assertIsInstance(parse_step(project, unmasked), OxidationParams)

        invalid = (
            ("missing timeHours", {**step, "nativeParameters": {"temperatureC": 1000}}),
            ("missing nativeParameters", {k: v for k, v in step.items() if k != "nativeParameters"}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
            ("mask type", {**step, "nativeParameters": {**step["nativeParameters"], "maskMaterial": 5}}),
            ("string time", {**step, "nativeParameters": {**step["nativeParameters"], "timeHours": "0.001"}}),
            ("bool time", {**step, "nativeParameters": {**step["nativeParameters"], "timeHours": True}}),
            ("nan time", {**step, "nativeParameters": {**step["nativeParameters"], "timeHours": float("nan")}}),
            ("zero time", {**step, "nativeParameters": {**step["nativeParameters"], "timeHours": 0.0}}),
            ("pressure", {**step, "nativeParameters": {**step["nativeParameters"], "pressureAtm": 0.0}}),
            ("oxidant", {**step, "nativeParameters": {**step["nativeParameters"], "oxidant": "dry"}}),
            ("silicon", {**step, "nativeParameters": {**step["nativeParameters"], "siliconMaterial": ""}}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = OxidationNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=oxidation_project(),
        )
        self.assertEqual(variant.validate(ctx, OxidationParams()), [])

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
        """Register, resolve, and execute the plugin without the shared lock."""
        dimension = int(project["dimension"])
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=GRID_DELTA),
            materials=MaterialState(
                order=material_order(domain),
                names={int(ps.Material.Si): "Si"},
            ),
        )
        context = ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="oxidation",
                variant_id="native",
                params=params,
                context=context,
                state=state,
                compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
            ),
            self.registry,
            _cpu_provider(ps),
        )
        return plan.run()

    def _assert_parity(self, reference: Any, candidate: Any, dimension: int) -> dict[str, Any]:
        result = compare_domains(reference, candidate, dimension)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(result["oldMaterialMap"], list(material_order(candidate)))
        self.assertGreater(result["oldPointCount"], 0)
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        return result

    def test_two_dimensional_plane_dry_matches_the_native_reference(self) -> None:
        project = oxidation_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)

        delta = outcome.delta
        self.assertIsInstance(outcome.result, StepResult)
        self.assertIs(delta, outcome.result.delta)
        self.assertIsInstance(delta.geometry, LevelSetState)
        self.assertIs(delta.geometry.handle, new_domain)
        self.assertEqual(delta.geometry.dimension, 2)
        self.assertEqual(delta.geometry.grid_delta, GRID_DELTA)
        self.assertIsInstance(delta.materials, MaterialState)
        order = material_order(new_domain)
        self.assertEqual(delta.materials.order, order)
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.Oxidation")
        self.assertEqual(metadata["operation"], "native")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertAlmostEqual(metadata["durationSeconds"], 3.6, places=9)
        self.assertEqual(metadata["durationDerivation"], "nativeParameters.timeHours * 3600.0")
        self.assertEqual(metadata["oxidant"], "Dry")
        self.assertEqual(metadata["oxidantNative"], "DRY" if hasattr(ps.OxidantType, "DRY") else "Dry")
        self.assertEqual(metadata["orientation"], "Si100")
        self.assertEqual(metadata["siliconMaterial"], "Si")
        self.assertEqual(metadata["oxideMaterial"], "SiO2")
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertIsNone(metadata["rayTracing"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["levelSetCount"], 2)
        self.assertEqual(metadata["materialMap"], list(order))

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "oxidation")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_three_dimensional_plane_smoke(self) -> None:
        project = oxidation_project(3)
        candidate = plane_domain(3)
        before = _top(candidate, 3)

        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self.assertIs(new_domain, candidate)
        self.assertGreater(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.geometry.grid_delta, GRID_DELTA)
        order = material_order(new_domain)
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertIn(int(ps.Material.SiO2), order)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_wet_oxidation_matches_the_native_reference(self) -> None:
        project = oxidation_project(2, native_parameters={"oxidant": "Wet"})
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        self.assertEqual(outcome.metadata["oxidant"], "Wet")
        self.assertEqual(outcome.metadata["parameters"]["oxidant"], "Wet")
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_material_map_includes_the_created_oxide_level_set(self) -> None:
        project = oxidation_project(2)
        outcome = self._run(project, plane_domain(2))
        order = material_order(outcome.state.geometry.handle)
        oxide_id = int(ps.Material.SiO2)
        silicon_id = int(ps.Material.Si)

        self.assertEqual(order, (silicon_id, oxide_id))
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self.assertIn(oxide_id, outcome.metadata["materialMap"])
        self.assertEqual(outcome.metadata["levelSetCount"], 2)
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.delta.materials.names[oxide_id], "SiO2")
        self.assertEqual(outcome.delta.materials.names[silicon_id], "Si")
        self.assertEqual(outcome.state.materials.names[oxide_id], "SiO2")

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_backend_or_native_module(self) -> None:
        code = (
            "import json\n"
            "import sys\n"
            "import plugins.oxidation\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'worker' or name.startswith('worker.')\n"
            "    or name == 'backend' or name.startswith('backend.')\n"
            ")\n"
            "print(json.dumps({'leaked': leaked, 'native': 'viennaps' in sys.modules}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"leaked": [], "native": False},
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
