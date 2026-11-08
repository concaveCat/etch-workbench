"""Tests for the native v2 TEOS thermal-deposition plugin.

Every execution test builds a small 2D/3D Si plane, registers the
``teosDeposition``/``native`` plugin in a **local**
:class:`~process_models.v2.registry.ProcessRegistry`, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` holding the test module
proxy. The shared plugin registry, ``load_registry()``, ``run_v2_step()`` and
``plugins.lock`` are never consulted: adding an in-tree plugin makes the
checked-in lock stale until it is regenerated, which is out of scope here.

The result is compared with a direct ``ps.TEOSDeposition`` + ``ps.Process``
reference through :func:`worker.process_models_v2.compare_domains`. That
reference duplicates the top level set as SiO2 first, exactly like the plugin
and the stage-8 direct-reference deposit path, because the native model grows
the new SiO2 level set rather than converting the substrate.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU_DISK flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. No server or worker process is involved; the v2 state store writes
into ``var/test-plugin-teosDeposition/``.
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
from plugins.teosDeposition import (
    TEOSDepositionNativeVariant,
    TEOSDepositionParams,
    TEOSDepositionProcessSpec,
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
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-teosDeposition"
PLUGIN_TOML = ROOT / "plugins" / "teosDeposition" / "plugin.toml"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828
ENGINE_FINGERPRINT = "test-teosDeposition"

#: ``backend.specialized_models`` defaults for ``teosDeposition``.
CATALOG_DEFAULTS = {
    "stickingProbabilityP1": 0.5,
    "rateP1": 0.05,
    "orderP1": 1.0,
    "stickingProbabilityP2": 0.0,
    "rateP2": 0.0,
    "orderP2": 0.0,
}

EXPLICIT_PARAMETERS = {
    "stickingProbabilityP1": 0.8,
    "rateP1": 0.08,
    "orderP1": 1.5,
    "stickingProbabilityP2": 0.25,
    "rateP2": 0.01,
    "orderP2": 0.5,
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


def teos_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_teosDeposition",
        "name": "TEOS",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "teosDeposition",
        "operation": "deposit",
        "depositMaterial": "SiO2",
        "duration": duration,
        "nativeParameters": parameters,
    }


def teos_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = 0.5,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"teosDeposition plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [teos_step(duration=duration, native_parameters=native_parameters)],
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
    *,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the TEOS deposit path directly, with no worker or plugin code.

    The top level set is duplicated as SiO2 before ``ps.Process`` is built,
    because ``TEOSDeposition`` deposits onto that new level set.
    """
    step = project["steps"][0]
    values = step["nativeParameters"]
    domain.duplicateTopLevelSet(ps_module.Material.SiO2)
    model = ps_module.TEOSDeposition(
        values["stickingProbabilityP1"],
        values["rateP1"],
        values["orderP1"],
        values["stickingProbabilityP2"],
        values["rateP2"],
        values["orderP2"],
    )
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
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
                    kernels=("CPU_DISK", "CPU_TRIANGLE"),
                ),
                handle=handle,
                fingerprint=ENGINE_FINGERPRINT,
            )
        ]
    )


class TEOSDepositionPluginTest(unittest.TestCase):
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

    def test_manifest_and_identity_match_the_contract(self) -> None:
        manifest = tomllib.loads(PLUGIN_TOML.read_text(encoding="utf-8"))
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "teosDeposition")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.teosDeposition:register")

        self.assertEqual(TEOSDepositionProcessSpec.id, "teosDeposition")
        self.assertEqual(TEOSDepositionProcessSpec.version, "1.0.0")
        self.assertEqual(TEOSDepositionProcessSpec.params_model, TEOSDepositionParams)
        self.assertEqual(TEOSDepositionProcessSpec.dimensions, (2, 3))
        self.assertEqual(TEOSDepositionProcessSpec.operations, ("deposit",))
        self.assertEqual(TEOSDepositionProcessSpec.default_variant, "native")
        self.assertIs(TEOSDepositionProcessSpec.parse_step, parse_step)

        variant = TEOSDepositionNativeVariant()
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
        self.assertEqual(
            profiles,
            {("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE")},
        )
        # The flat TEOSDeposition constructor has no GPU model conversion in
        # the installed 4.7.0 runtime, so it must not advertise GPU_DISK or
        # GPU_TRIANGLE.
        self.assertNotIn(("gpu", "GPU_DISK"), profiles)
        self.assertNotIn(("gpu", "GPU_TRIANGLE"), profiles)
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "teosDeposition")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("teosDeposition", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("teosDeposition"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("teosDeposition")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(list(CATALOG_DEFAULTS), list(TEOSDepositionParams.model_fields)[1:])

        params = TEOSDepositionParams(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = TEOSDepositionParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertNotIn("coverageTolerance", TEOSDepositionParams.model_fields)
        self.assertFalse(hasattr(params, "coverageTolerance"))
        self.assertEqual(params.flat_arguments(), tuple(CATALOG_DEFAULTS.values()))
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"stickingProbabilityP1": -0.1},
            {"stickingProbabilityP1": 1.1},
            {"stickingProbabilityP1": float("inf")},
            {"stickingProbabilityP2": -0.1},
            {"stickingProbabilityP2": 1.1},
            {"rateP1": -0.1},
            {"rateP1": float("inf")},
            {"orderP1": -0.1},
            {"rateP2": -0.1},
            {"orderP2": -0.1},
            {"coverageTolerance": 1e-3},
            {"unexpected": 1},
            {"rateP1": "0.05"},
            {"rateP1": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    TEOSDepositionParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = teos_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, TEOSDepositionParams)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        explicit = teos_project(native_parameters=EXPLICIT_PARAMETERS)
        params = parse_step(explicit, explicit["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), EXPLICIT_PARAMETERS)
        self.assertEqual(params.flat_arguments(), tuple(EXPLICIT_PARAMETERS.values()))

        sparse = teos_project(native_parameters={})
        sparse["steps"][0]["nativeParameters"] = {"rateP1": 0.2}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.rateP1, 0.2)
        self.assertEqual(params.stickingProbabilityP1, 0.5)
        self.assertEqual(params.orderP2, 0.0)

        without_material = teos_project()
        del without_material["steps"][0]["depositMaterial"]
        self.assertIsInstance(parse_step(without_material, without_material["steps"][0]), TEOSDepositionParams)

        claimed = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertIs(claimed[0], TEOSDepositionProcessSpec)
        self.assertIsInstance(claimed[1], TEOSDepositionParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = teos_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"precursors": {"rate": 0.1}}
        unknown = mutated()
        unknown["nativeParameters"]["coverageTolerance"] = 1e-3
        other_model = mutated(nativeModel="teospecvd")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="etch")
        other_material = mutated(depositMaterial="Si3N4")
        null_material = mutated(depositMaterial=None)

        for label, candidate in (
            ("advanced", advanced),
            ("coverageTolerance", unknown),
            ("teospecvd", other_model),
            ("isotropic", not_specialized),
            ("etch", other_operation),
            ("Si3N4", other_material),
            ("null material", null_material),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("zero duration", mutated(duration=0.0)),
            ("negative duration", mutated(duration=-1.0)),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("nan duration", mutated(duration=float("nan"))),
            (
                "negative sticking",
                {**step, "nativeParameters": {**step["nativeParameters"], "stickingProbabilityP1": -0.1}},
            ),
            (
                "sticking above one",
                {**step, "nativeParameters": {**step["nativeParameters"], "stickingProbabilityP2": 1.1}},
            ),
            ("negative rate", {**step, "nativeParameters": {**step["nativeParameters"], "rateP1": -0.1}}),
            ("negative order", {**step, "nativeParameters": {**step["nativeParameters"], "orderP2": -0.1}}),
            ("bool rate", {**step, "nativeParameters": {**step["nativeParameters"], "rateP1": True}}),
            ("nan rate", {**step, "nativeParameters": {**step["nativeParameters"], "rateP2": float("nan")}}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = TEOSDepositionNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=teos_project(),
        )
        self.assertEqual(variant.validate(ctx, TEOSDepositionParams(duration=1.0)), [])

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        project: dict[str, Any],
        domain: Any,
        *,
        kernel: str = "CPU_DISK",
    ) -> Any:
        """Register, resolve, and execute the plugin without the shared lock."""
        dimension = int(project["dimension"])
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=project["domain"]["gridDelta"]),
            materials=MaterialState(
                order=material_order(domain),
                names={int(ps.Material.Si): "Si"},
            ),
        )
        context = ProcessContext(
            dimension=dimension,
            grid_delta=float(project["domain"]["gridDelta"]),
            project=project,
            step=step,
            seed=RAY_SEED,
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="teosDeposition",
                variant_id="native",
                params=params,
                context=context,
                state=state,
                compute=ComputePolicy(backend="cpu", kernel=kernel),
            ),
            self.registry,
            _cpu_provider(self.ps),
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

    def _assert_sio2_name(self, materials: MaterialState) -> None:
        sio2_id = int(ps.Material.SiO2)
        self.assertEqual(materials.name_for(sio2_id), "SiO2")
        self.assertEqual(materials.id_for("SiO2"), sio2_id)
        self.assertEqual(materials.name_for(int(ps.Material.Si)), "Si")

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = teos_project(2, duration=0.5)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        after = _top(new_domain, 2)
        self.assertGreater(after, before)

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
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.SiO2)))
        self._assert_sio2_name(delta.materials)
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.TEOSDeposition")
        self.assertEqual(metadata["nativeConstructor"], "six-argument constructor")
        self.assertEqual(metadata["operation"], "deposit")
        self.assertEqual(metadata["depositMaterial"], "SiO2")
        self.assertTrue(metadata["duplicateTopLevelSet"])
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], 0.5)
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["rayTracing"]["rngSeed"], RAY_SEED)
        self.assertFalse(metadata["rayTracing"]["useRandomSeeds"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 2)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "teosDeposition")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = teos_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.2)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertGreater(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.geometry.grid_delta, grid_delta)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self._assert_sio2_name(outcome.delta.materials)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_explicit_parameter_values_are_applied_and_recorded(self) -> None:
        project = teos_project(2, native_parameters=EXPLICIT_PARAMETERS, duration=0.3)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(
            outcome.metadata["parameters"], {name: float(value) for name, value in EXPLICIT_PARAMETERS.items()}
        )
        self.assertEqual(outcome.metadata["duration"], 0.3)
        self._assert_sio2_name(outcome.delta.materials)

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = teos_project(2, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = teos_project(2)
        del project["solver"]
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        context = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
            seed=RAY_SEED,
        )
        state = ProcessState(
            geometry=LevelSetState(handle=plane_domain(2), dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=(int(ps.Material.Si),), names={int(ps.Material.Si): "Si"}),
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="teosDeposition",
                variant_id="native",
                params=params,
                context=context,
                state=state,
            ),
            self.registry,
            _cpu_provider(self.ps),
        )
        with self.assertRaises(RuntimeError) as raised:
            plan.run()
        self.assertIn("solver", str(raised.exception))

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.teosDeposition\n"
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
