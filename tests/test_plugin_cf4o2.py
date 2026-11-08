"""Tests for the native v2 CF4/O2 plasma-etch plugin.

Each execution test builds one small 2D/3D level-set domain, registers the
``cf4o2``/``native`` plugin in a **local** :class:`ProcessRegistry`, and runs it
through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping the live
``viennaps`` module. The shared plugin registry and ``plugins.lock`` are never
consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.CF4O2Etching`` + ``ps.Process``
reference through :func:`worker.process_models_v2.compare_domains`.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU_DISK flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. No server or worker process is involved; the v2 state store writes
into ``var/test-plugin-cf4o2/``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import model_spec
from plugins.cf4o2 import (
    CF4O2NativeVariant,
    CF4O2Params,
    CF4O2ProcessSpec,
    parse_step,
    register,
)
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import configure_advection, configure_rays
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-cf4o2"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828

#: ``backend.specialized_models.CF4O2_FLAT_FIELDS`` defaults for ``cf4o2``.
CATALOG_DEFAULTS = {
    "ionFlux": 1.0,
    "etchantFlux": 1.0,
    "oxygenFlux": 0.2,
    "polymerFlux": 0.2,
    "meanIonEnergy": 100.0,
    "sigmaIonEnergy": 10.0,
    "ionExponent": 100.0,
    "oxySputterYield": 3.0,
    "polySputterYield": 3.0,
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


def cf4o2_step(
    *,
    duration: Any = 0.2,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_cf4o2",
        "name": "CF4/O2",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "cf4o2",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def cf4o2_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    geometry: dict[str, Any] | None = None,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = 0.2,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"cf4o2 plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [cf4o2_step(duration=duration, native_parameters=native_parameters)],
        "solver": _solver(),
    }


def plane_domain(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
) -> Any:
    ps.setDimension(dimension)
    if dimension == 2:
        domain = ps.Domain(
            gridDelta=grid_delta,
            xExtent=x_extent,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    else:
        domain = ps.Domain(
            gridDelta=grid_delta,
            xExtent=x_extent,
            yExtent=x_extent,
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
    """Build the flat CF4O2Etching path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    model = ps_module.CF4O2Etching(
        values["ionFlux"],
        values["etchantFlux"],
        values["oxygenFlux"],
        values["polymerFlux"],
        values["meanIonEnergy"],
        values["sigmaIonEnergy"],
        values["ionExponent"],
        values["oxySputterYield"],
        values["polySputterYield"],
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


class CF4O2PluginTest(unittest.TestCase):
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

    def test_identity_matches_the_manifest_contract(self) -> None:
        self.assertEqual(CF4O2ProcessSpec.id, "cf4o2")
        self.assertEqual(CF4O2ProcessSpec.version, "1.0.0")
        self.assertEqual(CF4O2ProcessSpec.params_model, CF4O2Params)
        self.assertEqual(CF4O2ProcessSpec.dimensions, (2, 3))
        self.assertEqual(CF4O2ProcessSpec.operations, ("etch",))
        self.assertEqual(CF4O2ProcessSpec.default_variant, "native")
        self.assertIs(CF4O2ProcessSpec.parse_step, parse_step)

        variant = CF4O2NativeVariant()
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
        self.assertEqual(
            tuple((profile.backend, profile.kernel) for profile in variant.engine.profiles),
            (("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE")),
        )
        # The flat CF4O2Etching constructor has no GPU model conversion in the
        # installed 4.7.0 runtime; the advanced parameter-object path is not
        # implemented by this plugin, so it must not advertise GPU_DISK or
        # GPU_TRIANGLE.
        self.assertNotIn("GPU_DISK", {profile.kernel for profile in variant.engine.profiles})
        self.assertNotIn("GPU_TRIANGLE", {profile.kernel for profile in variant.engine.profiles})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "cf4o2")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("cf4o2", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("cf4o2")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(list(CATALOG_DEFAULTS), list(CF4O2Params.model_fields)[1:-1])

        params = CF4O2Params(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = CF4O2Params.model_fields[name]
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
            {"polymerFlux": float("inf")},
            {"meanIonEnergy": 1_000_001.0},
            {"polySputterYield": -1.0},
            {"etchStopDepth": -1_000_001.0},
            {"coverageTolerance": -0.1},
            {"coverageTolerance": 1.1},
            {"coverageTolerance": float("nan")},
            {"unexpected": 1},
            {"ionFlux": "1.0"},
            {"ionFlux": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    CF4O2Params(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = cf4o2_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, CF4O2Params)
        assert params is not None
        self.assertEqual(params.duration, 0.2)
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)
        self.assertIsNone(params.coverageTolerance)

        with_coverage = cf4o2_project(native_parameters={"coverageTolerance": 0.05, "polymerFlux": 0.7})
        params = parse_step(with_coverage, with_coverage["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.coverageTolerance, 0.05)
        self.assertEqual(params.polymerFlux, 0.7)
        self.assertEqual(params.oxygenFlux, 0.2)

        sparse = cf4o2_project(native_parameters={})
        sparse["steps"][0]["nativeParameters"] = {"ionFlux": 2}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.ionFlux, 2.0)
        self.assertEqual(params.etchantFlux, 1.0)

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], CF4O2ProcessSpec)
        self.assertIsInstance(resolved[1], CF4O2Params)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = cf4o2_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        unknown = mutated()
        unknown["nativeParameters"]["polyFlux"] = 0.2
        other_model = mutated(nativeModel="sf6o2")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="deposit")

        for label, candidate in (
            ("advanced", advanced),
            ("unknown", unknown),
            ("sf6o2", other_model),
            ("isotropic", not_specialized),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))
        # Dispatch is ``model``/``nativeModel`` only, as specified. The project
        # validator rejects a deposit-labelled cf4o2 step before dispatch, so
        # this process still only ever runs as an etch in a validated project.
        self.assertIsInstance(parse_step(project, other_operation), CF4O2Params)

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
        variant = CF4O2NativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=cf4o2_project(),
        )
        self.assertEqual(variant.validate(ctx, CF4O2Params(duration=1.0)), [])

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        project: dict[str, Any],
        domain: Any,
        *,
        kernel: str = "CPU_DISK",
    ) -> Any:
        """Run the registered plugin through a local registry and static engine.

        ``registry`` is the test's own :class:`ProcessRegistry`, so the shared
        plugin lock is never read. The engine handle is the seeded module proxy,
        exactly like the reference run.
        """
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        spec, params = resolved

        context = ProcessContext(
            dimension=int(project["dimension"]),
            grid_delta=float(project["domain"]["gridDelta"]),
            project=project,
            step=step,
            seed=RAY_SEED,
        )
        state = ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=context.dimension,
                grid_delta=context.grid_delta,
            ),
            materials=MaterialState(order=material_order(domain)),
        )
        provider = StaticEngineProvider(
            [
                StaticEngine(
                    capabilities=EngineCapabilities(
                        engine_id="viennaps",
                        version=VIENNAPS_VERSION,
                        kind="native",
                        backends=("cpu",),
                        kernels=("CPU_DISK", "CPU_TRIANGLE"),
                    ),
                    handle=self.ps,
                    fingerprint="cf4o2-plugin-test",
                )
            ]
        )
        store = StateStore(self.work_dir / kernel / step["id"])
        flow = FlowExecutor(self.registry, provider, store).run(
            state,
            [
                FlowStep(
                    step_id=step["id"],
                    process_id=spec.id,
                    variant_id=self.registry.default_variant_id(spec.id),
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
        project = cf4o2_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference, coverage_tolerance=1e-3)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIsNot(new_domain, candidate)
        # The executor checkpoints the mutating variant: the input is untouched.
        self.assertTrue(compare_domains(plane_domain(2), candidate, 2)["matched"])
        after = _top(new_domain, 2)
        self.assertLess(after, before)

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
        self.assertEqual(delta.materials.order, (int(ps.Material.Si),))
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.CF4O2Etching")
        self.assertEqual(metadata["nativeConstructor"], "flat ten-argument overload")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], 0.2)
        self.assertEqual(metadata["coverageTolerance"], 1e-3)
        self.assertIsNone(metadata["coverageToleranceRequested"])
        self.assertEqual(metadata["coverageToleranceSource"], "workbench-default")
        self.assertEqual(metadata["coverage"]["tolerance"], 1e-3)
        self.assertIsNone(metadata["coverage"]["requested"])
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["rays"]["raysPerPoint"], RAYS_PER_POINT)
        self.assertEqual(metadata["rays"]["rngSeed"], RAY_SEED)
        self.assertFalse(metadata["rays"]["useRandomSeeds"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 1)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "cf4o2")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "cf4o2-plugin-test")

    def test_masked_trench_matches_the_native_reference(self) -> None:
        project = cf4o2_project(
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

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = cf4o2_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.1)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
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
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

    def test_explicit_coverage_tolerance_is_applied_and_recorded(self) -> None:
        project = cf4o2_project(2, native_parameters={"coverageTolerance": 0.05}, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, coverage_tolerance=0.05)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["coverageTolerance"], 0.05)
        self.assertEqual(outcome.metadata["coverageToleranceRequested"], 0.05)
        self.assertEqual(outcome.metadata["coverageToleranceSource"], "param")
        self.assertEqual(outcome.metadata["coverage"]["tolerance"], 0.05)
        self.assertEqual(outcome.metadata["coverage"]["source"], "param")

    def test_zero_coverage_tolerance_uses_the_native_iteration_policy(self) -> None:
        project = cf4o2_project(2, native_parameters={"coverageTolerance": 0.0}, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, coverage_tolerance=0.0)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["coverageTolerance"], 0.0)
        self.assertEqual(outcome.metadata["coverageToleranceSource"], "param")

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = cf4o2_project(2, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, coverage_tolerance=1e-3, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = cf4o2_project(2)
        del project["solver"]
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        context = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project, step=step, seed=RAY_SEED)
        variant = CF4O2NativeVariant()
        engine = SimpleNamespace(profile=SimpleNamespace(backend="cpu", kernel="CPU_DISK"), handle=ps)
        state = ProcessState(
            geometry=LevelSetState(handle=plane_domain(2), dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=(int(ps.Material.Si),)),
        )
        with self.assertRaises(RuntimeError) as raised:
            variant.execute(state, resolved[1], context, engine)
        self.assertIn("solver", str(raised.exception))

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.cf4o2\n"
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
