"""Tests for the native v2 SF6/C4F8 plasma-etch plugin.

Each test builds one small 2D/3D level-set domain and runs the real
``sf6c4f8``/``native`` registration through a **manual** process registry plus a
static native engine provider whose handle is the installed ``viennaps``
module. Nothing here calls :func:`worker.process_models_v2.load_registry`, so
the shared ``plugins.lock`` is never consulted: adding this plugin does not
make the lock stale for the rest of the suite.

The plugin path goes through :class:`~process_models.v2.executor.FlowExecutor`
(manual registry + ``StaticEngineProvider``), which checkpoints the
``mutates_input`` geometry exactly like the worker bridge. Its result is
compared with a direct ``ps.SF6C4F8Etching`` + ``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU_DISK flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. Per-step persistence writes into ``var/test-plugin-sf6c4f8/``.
"""

from __future__ import annotations

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
from plugins.sf6c4f8 import (
    SF6C4F8NativeVariant,
    SF6C4F8Params,
    SF6C4F8ProcessSpec,
    parse_step,
    register,
)
from plugins.sf6c4f8.params import WORKBENCH_COVERAGE_TOLERANCE
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import ViennaPSMaterialRegistry, configure_advection, configure_rays
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from process_models.v2.state_backend import HandleStateBackend
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-sf6c4f8"

GRID_DELTA = 0.2
X_EXTENT = 4.0
X_EXTENT_3D = 2.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828

#: ``backend.specialized_models.SF6C4F8_FLAT_FIELDS`` defaults (argument order
#: of the native flat overload).
CATALOG_DEFAULTS = {
    "ionFlux": 1.0,
    "etchantFlux": 1.0,
    "meanEnergy": 100.0,
    "sigmaEnergy": 10.0,
    "ionExponent": 300.0,
    "etchStopDepth": -1_000_000.0,
}


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "raysPerPoint": RAYS_PER_POINT,
        "cpuThreads": 1,
        "random": {"mode": "fixed", "seed": RAY_SEED},
        "enginePreference": "cpu",
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
        "rayTracing": {
            "diskRadius": 0.0,
            "ignoreFluxBoundaries": False,
            "maxBoundaryHits": 80,
            "maxReflections": 3,
            "minNodeDistanceFactor": 0.05,
            "normalizationType": "SOURCE",
            "smoothingNeighbors": 1,
        },
    }


def sf6c4f8_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_sf6c4f8",
        "name": "SF6/C4F8",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "sf6c4f8",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def sf6c4f8_project(
    dimension: int = 2,
    *,
    geometry: dict[str, Any] | None = None,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = 0.5,
    extent: float | None = None,
) -> dict[str, Any]:
    extent = X_EXTENT if extent is None else float(extent)
    domain: dict[str, Any] = {"gridDelta": GRID_DELTA, "xExtent": extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"sf6c4f8 plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [sf6c4f8_step(duration=duration, native_parameters=native_parameters)],
        "solver": _solver(),
    }


def plane_domain(dimension: int = 2, *, extent: float | None = None) -> Any:
    extent = X_EXTENT if extent is None else float(extent)
    ps.setDimension(dimension)
    if dimension == 2:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=extent,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    else:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=extent,
            yExtent=extent,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def trench_domain() -> Any:
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakeTrench(
        domain=domain,
        trenchWidth=1.6,
        trenchDepth=1.2,
        trenchTaperAngle=0.0,
        maskHeight=0.6,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Mask,
    ).apply()
    return domain


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


class _SeededModule:
    """Module proxy that pins the native ray tracing on every ``Process``.

    ``RayTracingParameters.useRandomSeeds`` defaults to ``True`` in the
    installed binding, so two CPU_DISK runs draw different rays and cannot be
    compared bit-for-bit. Using one proxy for both the reference and the plugin
    path keeps the comparison exact without changing the plugin.
    """

    def __init__(self, module: Any) -> None:
        self._module = module

    def Process(self, *args: Any, **kwargs: Any) -> Any:
        process = self._module.Process(*args, **kwargs)
        rays = self._module.RayTracingParameters()
        rays.raysPerPoint = RAYS_PER_POINT
        rays.rngSeed = RAY_SEED
        rays.useRandomSeeds = False
        process.setParameters(rays)
        return process

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)


def _reference(
    ps_module: Any,
    project: dict[str, Any],
    domain: Any,
    coverage_tolerance: float,
    *,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the flat SF6C4F8Etching path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    model = ps_module.SF6C4F8Etching(
        values["ionFlux"],
        values["etchantFlux"],
        values["meanEnergy"],
        values["sigmaEnergy"],
        values["ionExponent"],
        values["etchStopDepth"],
    )
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
    coverage = ps_module.CoverageParameters()
    coverage.tolerance = coverage_tolerance
    process.setParameters(coverage)
    process.apply()
    return domain


class SF6C4F8PluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.ps = _SeededModule(ps)
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)
        try:
            TEST_ROOT.rmdir()
        except OSError:
            pass

    # -- identity / parameters --------------------------------------------

    def test_plugin_manifest_declares_the_expected_entrypoint(self) -> None:
        with (ROOT / "plugins" / "sf6c4f8" / "plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "sf6c4f8")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.sf6c4f8:register")

    def test_identity_matches_the_manifest_contract(self) -> None:
        self.assertEqual(SF6C4F8ProcessSpec.id, "sf6c4f8")
        self.assertEqual(SF6C4F8ProcessSpec.version, "1.0.0")
        self.assertEqual(SF6C4F8ProcessSpec.params_model, SF6C4F8Params)
        self.assertEqual(SF6C4F8ProcessSpec.dimensions, (2, 3))
        self.assertEqual(SF6C4F8ProcessSpec.operations, ("etch",))
        self.assertEqual(SF6C4F8ProcessSpec.default_variant, "native")
        self.assertIs(SF6C4F8ProcessSpec.parse_step, parse_step)

        variant = SF6C4F8NativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertTrue(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertFalse(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        profiles = {(profile.backend, profile.kernel) for profile in variant.engine.profiles}
        self.assertEqual(
            profiles,
            {("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE"), ("gpu", "GPU_DISK")},
        )
        # No GPU_TRIANGLE: the worker path has no evidence for that kernel.
        self.assertNotIn(("gpu", "GPU_TRIANGLE"), profiles)
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "sf6c4f8")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("sf6c4f8", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("sf6c4f8")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertIn("coverageTolerance", {field["key"] for field in spec["optionalFields"]})

        params = SF6C4F8Params(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = SF6C4F8Params.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertIsNone(params.coverageTolerance)
        self.assertEqual(params.flat_arguments(), tuple(CATALOG_DEFAULTS.values()))
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"ionFlux": -0.1},
            {"ionFlux": float("inf")},
            {"meanEnergy": 1_000_001.0},
            {"etchStopDepth": -1_000_001.0},
            {"coverageTolerance": -0.1},
            {"coverageTolerance": 1.1},
            {"coverageTolerance": float("nan")},
            {"unexpected": 1},
            {"passivationFlux": 0.2},
            {"ionFlux": "1.0"},
            {"ionFlux": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    SF6C4F8Params(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = sf6c4f8_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, SF6C4F8Params)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)
        self.assertIsNone(params.coverageTolerance)

        with_coverage = sf6c4f8_project(native_parameters={"coverageTolerance": 0.05, "ionExponent": 120.0})
        params = parse_step(with_coverage, with_coverage["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.coverageTolerance, 0.05)
        self.assertEqual(params.ionExponent, 120.0)
        self.assertEqual(params.ionFlux, 1.0)

        sparse = sf6c4f8_project(native_parameters={})
        sparse["steps"][0]["nativeParameters"] = {"ionFlux": 2}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.ionFlux, 2.0)
        self.assertEqual(params.etchantFlux, 1.0)
        self.assertEqual(params.meanEnergy, 100.0)

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], SF6C4F8ProcessSpec)
        self.assertIsInstance(resolved[1], SF6C4F8Params)

    def test_parse_step_rejects_other_processes_advanced_and_passivation(self) -> None:
        project = sf6c4f8_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        advanced_passivation = mutated()
        advanced_passivation["nativeParameters"]["advanced"] = {
            "passivation": {"A_ie": 1.0},
        }
        unknown = mutated()
        unknown["nativeParameters"]["polymerFlux"] = 0.2
        passivation_flux = mutated()
        passivation_flux["nativeParameters"]["passivationFlux"] = 0.2
        zero_passivation_flux = mutated()
        zero_passivation_flux["nativeParameters"]["passivationFlux"] = 0.0
        other_model = mutated(nativeModel="sf6o2")
        other_specialized = mutated(nativeModel="cf4o2")
        not_specialized = mutated(model="isotropic")

        for label, candidate in (
            ("advanced", advanced),
            ("advanced-passivation", advanced_passivation),
            ("unknown", unknown),
            ("passivationFlux", passivation_flux),
            ("passivationFlux-zero", zero_passivation_flux),
            ("sf6o2", other_model),
            ("cf4o2", other_specialized),
            ("isotropic", not_specialized),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))
        # Dispatch is ``model``/``nativeModel`` only, as specified. The project
        # validator rejects a deposit-labelled sf6c4f8 step before dispatch, so
        # this process still only ever runs as an etch in a validated project.
        other_operation = mutated(operation="deposit")
        self.assertIsInstance(parse_step(project, other_operation), SF6C4F8Params)

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("string duration", mutated(duration="1.0")),
            ("bool flux", {**step, "nativeParameters": {**step["nativeParameters"], "ionFlux": True}}),
            ("nan flux", {**step, "nativeParameters": {**step["nativeParameters"], "ionFlux": float("nan")}}),
            ("tolerance", {**step, "nativeParameters": {**step["nativeParameters"], "coverageTolerance": 2.0}}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = SF6C4F8NativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=sf6c4f8_project(),
        )
        self.assertEqual(variant.validate(ctx, SF6C4F8Params(duration=1.0)), [])

    def test_flat_constructor_matches_the_worker_model_builder(self) -> None:
        from backend.specialized_models import advanced_spec
        from worker.simulate import MATERIAL_IDS, material_handle
        from worker.specialized import build_specialized_model

        project = sf6c4f8_project(2, native_parameters={"ionExponent": 120.0, "etchStopDepth": -0.5})
        step = project["steps"][0]
        worker_model, extras, _actual, duration = build_specialized_model(
            ps,
            plane_domain(2),
            project,
            step,
            lambda name: material_handle(ps, name),
            MATERIAL_IDS,
        )
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        assert params is not None
        plugin_model = ps.SF6C4F8Etching(*params.flat_arguments())

        self.assertEqual(type(worker_model).__name__, "SF6C4F8Etching")
        self.assertEqual(duration, params.duration)
        self.assertEqual(len(extras), 1)
        self.assertEqual(extras[0].tolerance, WORKBENCH_COVERAGE_TOLERANCE)

        mapping = advanced_spec("sf6c4f8")["flatMapping"]
        self.assertEqual(set(mapping), set(CATALOG_DEFAULTS))

        def read(parameters: Any, path: str) -> float:
            for part in path.split("."):
                parameters = getattr(parameters, part)
            return float(parameters)

        worker_parameters = worker_model.getParameters()
        plugin_parameters = plugin_model.getParameters()
        for name, path in mapping.items():
            with self.subTest(field=name):
                self.assertEqual(read(plugin_parameters, path), read(worker_parameters, path))
        self.assertEqual(read(plugin_parameters, mapping["ionExponent"]), 120.0)
        self.assertEqual(read(plugin_parameters, mapping["etchStopDepth"]), -0.5)

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any, *, kernel: str = "CPU_DISK") -> Any:
        """Run the manual registry end to end without touching ``plugins.lock``."""
        step = project["steps"][0]
        dimension = int(project["dimension"])
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        spec, params = resolved

        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=GRID_DELTA),
            materials=MaterialState(order=material_order(domain)),
        )
        context = ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
            seed=RAY_SEED,
        )
        backend = HandleStateBackend(
            self.ps,
            id="viennaps",
            version=VIENNAPS_VERSION,
            material_registry=ViennaPSMaterialRegistry(self.ps),
        )
        engine = StaticEngine(
            capabilities=EngineCapabilities(
                engine_id="viennaps",
                version=VIENNAPS_VERSION,
                kind="native",
                backends=("cpu",),
                kernels=("CPU_DISK", "CPU_TRIANGLE"),
            ),
            handle=self.ps,
            fingerprint="test-plugin-sf6c4f8",
            state_backend=backend,
        )
        provider = StaticEngineProvider([engine])
        store = StateStore(self.work_dir / "v2" / kernel)
        flow = FlowExecutor(self.registry, provider, store).run(
            state,
            [
                FlowStep(
                    step_id=step["id"],
                    process_id=spec.id,
                    variant_id="native",
                    params=params,
                    context=context,
                    compute=ComputePolicy(backend="cpu", kernel=kernel),
                )
            ],
        )
        return flow.outcomes[-1]

    def _assert_parity(self, reference: Any, candidate: Any, dimension: int) -> dict[str, Any]:
        result = compare_domains(reference, candidate, dimension)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(result["oldMaterialMap"], list(material_order(candidate)))
        self.assertGreater(result["oldPointCount"], 0)
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        return result

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = sf6c4f8_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference, coverage_tolerance=1e-3)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIsNot(new_domain, candidate)
        # The executor checkpoints the mutating variant: the input handle is untouched.
        self.assertTrue(compare_domains(plane_domain(2), candidate, 2)["matched"])
        after = _top(new_domain, 2)
        self.assertLess(after, before)

        delta = outcome.delta
        self.assertIsInstance(delta.geometry, LevelSetState)
        self.assertIs(delta.geometry.handle, new_domain)
        self.assertEqual(delta.geometry.dimension, 2)
        self.assertEqual(delta.geometry.grid_delta, GRID_DELTA)
        self.assertIsInstance(delta.materials, MaterialState)
        order = material_order(new_domain)
        self.assertEqual(delta.materials.order, order)
        self.assertEqual(delta.materials.order, (int(ps.Material.Si),))
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.warnings, [])
        self.assertEqual(outcome.metadata["nativeClass"], "ps.SF6C4F8Etching")
        self.assertEqual(outcome.metadata["nativeConstructor"], "flat six-argument overload")
        self.assertEqual(outcome.metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(outcome.metadata["duration"], 0.5)
        self.assertEqual(outcome.metadata["coverageTolerance"], WORKBENCH_COVERAGE_TOLERANCE)
        self.assertIsNone(outcome.metadata["coverageToleranceRequested"])
        self.assertEqual(outcome.metadata["coverageToleranceSource"], "workbench-default")
        self.assertEqual(outcome.metadata["timeStepRatio"], 0.25)
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(outcome.metadata["dimension"], 2)
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "sf6c4f8")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "test-plugin-sf6c4f8")

    def test_masked_trench_matches_the_native_reference(self) -> None:
        project = sf6c4f8_project(
            2,
            geometry={
                "kind": "trench",
                "width": 1.6,
                "depth": 1.2,
                "taperAngle": 0.0,
                "maskHeight": 0.6,
                "maskTaperAngle": 0.0,
                "substrateMaterial": "Si",
                "maskMaterial": "Mask",
            },
            duration=0.2,
        )
        reference = trench_domain()
        candidate = trench_domain()

        _reference(self.ps, project, reference, coverage_tolerance=1e-3)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        order = material_order(new_domain)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = sf6c4f8_project(2, duration=0.2, extent=X_EXTENT_3D)
        reference = plane_domain(2, extent=X_EXTENT_3D)
        candidate = plane_domain(2, extent=X_EXTENT_3D)

        _reference(self.ps, project, reference, coverage_tolerance=1e-3, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")

        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_three_dimensional_plane_smoke(self) -> None:
        project = sf6c4f8_project(3, duration=0.2, extent=X_EXTENT_3D)
        reference = plane_domain(3, extent=X_EXTENT_3D)
        candidate = plane_domain(3, extent=X_EXTENT_3D)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference, coverage_tolerance=1e-3)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertLess(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

    def test_default_coverage_tolerance_uses_the_workbench_value(self) -> None:
        project = sf6c4f8_project(2, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, coverage_tolerance=WORKBENCH_COVERAGE_TOLERANCE)
        outcome = self._run(project, candidate)

        self._assert_parity(reference, outcome.state.geometry.handle, 2)
        self.assertEqual(outcome.metadata["coverageTolerance"], 1e-3)
        self.assertEqual(outcome.metadata["coverageTolerance"], WORKBENCH_COVERAGE_TOLERANCE)
        self.assertIsNone(outcome.metadata["coverageToleranceRequested"])
        self.assertEqual(outcome.metadata["coverageToleranceSource"], "workbench-default")

    def test_explicit_coverage_tolerance_is_applied_and_recorded(self) -> None:
        project = sf6c4f8_project(2, native_parameters={"coverageTolerance": 0.05}, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, coverage_tolerance=0.05)
        outcome = self._run(project, candidate)

        self._assert_parity(reference, outcome.state.geometry.handle, 2)
        self.assertEqual(outcome.metadata["coverageTolerance"], 0.05)
        self.assertEqual(outcome.metadata["coverageToleranceRequested"], 0.05)
        self.assertEqual(outcome.metadata["coverageToleranceSource"], "param")

    def test_incomplete_project_fails_loudly(self) -> None:
        project = sf6c4f8_project(2)
        del project["solver"]
        with self.assertRaises(RuntimeError) as raised:
            self._run(project, plane_domain(2))
        self.assertIn("solver", str(raised.exception))

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.sf6c4f8\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'worker' or name.startswith('worker.')\n"
            "    or name == 'backend' or name.startswith('backend.')\n"
            ")\n"
            "print(leaked)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
