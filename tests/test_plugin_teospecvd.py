"""Tests for the native v2 TEOS PECVD plugin.

Each execution test builds one small 2D/3D level-set domain, registers the
``teospecvd``/``native`` plugin in a **local** :class:`ProcessRegistry`, and
runs it through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping the
live ``viennaps`` module. The shared plugin registry and ``plugins.lock`` are
never consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.TEOSPECVD`` + ``ps.Process``
reference that also duplicates the SiO2 top level set, through
:func:`worker.process_models_v2.compare_domains`.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU_DISK flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. No server or worker process is involved; the v2 state store writes
into ``var/test-plugin-teospecvd/``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import model_spec
from plugins.teospecvd import (
    TEOSPECVDNativeVariant,
    TEOSPECVDParams,
    TEOSPECVDProcessSpec,
    parse_step,
    register,
)
from plugins.teospecvd.params import FLAT_FIELDS
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
TEST_ROOT = ROOT / "var" / "test-plugin-teospecvd"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828
DEPOSIT_MATERIAL = "SiO2"

#: ``backend.specialized_models`` defaults for the ``teospecvd`` catalog row.
CATALOG_DEFAULTS = {
    "stickingProbabilityRadical": 0.5,
    "depositionRateRadical": 0.05,
    "depositionRateIon": 0.02,
    "exponentIon": 1.0,
    "stickingProbabilityIon": 1.0,
    "reactionOrderRadical": 1.0,
    "reactionOrderIon": 1.0,
    "minAngleIon": 0.0,
}

#: Every flat field differs from its catalog default so argument-order mistakes
#: and ignored parameters both show up in the parity comparison.
EXPLICIT_PARAMETERS = {
    "stickingProbabilityRadical": 0.7,
    "depositionRateRadical": 0.08,
    "depositionRateIon": 0.03,
    "exponentIon": 2.0,
    "stickingProbabilityIon": 0.9,
    "reactionOrderRadical": 0.5,
    "reactionOrderIon": 1.5,
    "minAngleIon": 15.0,
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


def teospecvd_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
    deposit_material: Any = DEPOSIT_MATERIAL,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_teospecvd",
        "name": "TEOS PECVD",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "teospecvd",
        "operation": "deposit",
        "depositMaterial": deposit_material,
        "duration": duration,
        "nativeParameters": parameters,
    }


def teospecvd_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"teospecvd plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [teospecvd_step(duration=duration, native_parameters=native_parameters)],
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


def _sio2(module: Any) -> Any:
    return module.MaterialRegistry.instance().getMaterial(DEPOSIT_MATERIAL)


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
    """Build the flat TEOSPECVD path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    domain.duplicateTopLevelSet(_sio2(ps_module))
    model = ps_module.TEOSPECVD(*(values[name] for name in FLAT_FIELDS))
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
    process.apply()
    return domain


class TEOSPECVDPluginTest(unittest.TestCase):
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
        self.assertEqual(TEOSPECVDProcessSpec.id, "teospecvd")
        self.assertEqual(TEOSPECVDProcessSpec.version, "1.0.0")
        self.assertEqual(TEOSPECVDProcessSpec.params_model, TEOSPECVDParams)
        self.assertEqual(TEOSPECVDProcessSpec.dimensions, (2, 3))
        self.assertEqual(TEOSPECVDProcessSpec.operations, ("deposit",))
        self.assertEqual(TEOSPECVDProcessSpec.default_variant, "native")
        self.assertIs(TEOSPECVDProcessSpec.parse_step, parse_step)

        variant = TEOSPECVDNativeVariant()
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
        self.assertEqual(
            tuple((profile.backend, profile.kernel) for profile in variant.engine.profiles),
            (("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE"), ("gpu", "GPU_DISK")),
        )
        # Stage 9 evidence only covers GPU_DISK for TEOSPECVD.
        self.assertNotIn("GPU_TRIANGLE", {profile.kernel for profile in variant.engine.profiles})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "teospecvd")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("teospecvd", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("teospecvd")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(spec["depositMaterial"], DEPOSIT_MATERIAL)
        self.assertEqual(list(CATALOG_DEFAULTS), list(TEOSPECVDParams.model_fields)[1:])

        params = TEOSPECVDParams(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = TEOSPECVDParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertNotIn("coverageTolerance", TEOSPECVDParams.model_fields)
        self.assertNotIn("depositMaterial", TEOSPECVDParams.model_fields)
        self.assertEqual(params.flat_arguments(), tuple(CATALOG_DEFAULTS.values()))
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"stickingProbabilityRadical": -0.1},
            {"stickingProbabilityRadical": 1.1},
            {"stickingProbabilityIon": 1.1},
            {"depositionRateRadical": -0.1},
            {"depositionRateIon": float("inf")},
            {"exponentIon": -1.0},
            {"reactionOrderRadical": -0.5},
            {"reactionOrderIon": float("nan")},
            {"minAngleIon": -1.0},
            {"minAngleIon": 181.0},
            {"coverageTolerance": 0.1},
            {"depositMaterial": DEPOSIT_MATERIAL},
            {"unexpected": 1},
            {"stickingProbabilityRadical": "0.5"},
            {"depositionRateRadical": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    TEOSPECVDParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = teospecvd_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, TEOSPECVDParams)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        without_material = teospecvd_project()
        del without_material["steps"][0]["depositMaterial"]
        params = parse_step(without_material, without_material["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        sparse = teospecvd_project()
        sparse["steps"][0]["nativeParameters"] = {"depositionRateRadical": 0.07, "minAngleIon": 30}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.depositionRateRadical, 0.07)
        self.assertEqual(params.minAngleIon, 30.0)
        self.assertEqual(params.stickingProbabilityRadical, 0.5)

        missing_parameters = teospecvd_project()
        del missing_parameters["steps"][0]["nativeParameters"]
        params = parse_step(missing_parameters, missing_parameters["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), CATALOG_DEFAULTS)

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], TEOSPECVDProcessSpec)
        self.assertIsInstance(resolved[1], TEOSPECVDParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = teospecvd_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        unknown = mutated()
        unknown["nativeParameters"]["coverageTolerance"] = 0.1
        other_model = mutated(nativeModel="teosDeposition")
        not_specialized = mutated(model="isotropic")
        not_deposit = mutated(operation="etch")
        missing_operation = {key: value for key, value in step.items() if key != "operation"}
        other_material = mutated(depositMaterial="Si3N4")
        empty_material = mutated(depositMaterial="")
        null_material = mutated(depositMaterial=None)

        for label, candidate in (
            ("advanced", advanced),
            ("unknown", unknown),
            ("teosDeposition", other_model),
            ("isotropic", not_specialized),
            ("etch", not_deposit),
            ("missing operation", missing_operation),
            ("Si3N4", other_material),
            ("empty material", empty_material),
            ("null material", null_material),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        invalid = (
            ("missing duration", {key: value for key, value in step.items() if key != "duration"}),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("zero duration", mutated(duration=0.0)),
            ("negative duration", mutated(duration=-1.0)),
            ("nan duration", mutated(duration=float("nan"))),
            ("inf duration", mutated(duration=float("inf"))),
            (
                "string field",
                {**step, "nativeParameters": {**step["nativeParameters"], "depositionRateIon": "0.02"}},
            ),
            (
                "bool field",
                {**step, "nativeParameters": {**step["nativeParameters"], "exponentIon": True}},
            ),
            (
                "nan field",
                {**step, "nativeParameters": {**step["nativeParameters"], "minAngleIon": float("nan")}},
            ),
            (
                "negative rate",
                {**step, "nativeParameters": {**step["nativeParameters"], "depositionRateIon": -0.01}},
            ),
            (
                "sticking probability",
                {**step, "nativeParameters": {**step["nativeParameters"], "stickingProbabilityIon": 1.1}},
            ),
            (
                "angle",
                {**step, "nativeParameters": {**step["nativeParameters"], "minAngleIon": 181.0}},
            ),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = TEOSPECVDNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=teospecvd_project(),
        )
        self.assertEqual(variant.validate(ctx, TEOSPECVDParams(duration=1.0)), [])

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        project: dict[str, Any],
        domain: Any,
        *,
        kernel: str = "CPU_DISK",
        material_names: dict[int, str] | None = None,
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
        names = {int(ps.Material.Si): "Si"} if material_names is None else material_names
        state = ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=context.dimension,
                grid_delta=context.grid_delta,
            ),
            materials=MaterialState(order=material_order(domain), names=names),
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
                    fingerprint="teospecvd-plugin-test",
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
        project = teospecvd_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)
        self.assertAlmostEqual(before, 0.0, places=12)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIsNot(new_domain, candidate)
        # The executor checkpoints the mutating variant: the input is untouched.
        self.assertTrue(compare_domains(plane_domain(2), candidate, 2)["matched"])
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
        self.assertEqual(delta.materials.order, (int(ps.Material.Si), int(ps.Material.SiO2)))
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.TEOSPECVD")
        self.assertEqual(metadata["nativeConstructor"], "flat eight-argument overload")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], 0.5)
        self.assertEqual(metadata["depositMaterial"], DEPOSIT_MATERIAL)
        self.assertEqual(metadata["createdMaterialId"], int(ps.Material.SiO2))
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["rays"]["raysPerPoint"], RAYS_PER_POINT)
        self.assertEqual(metadata["rays"]["rngSeed"], RAY_SEED)
        self.assertFalse(metadata["rays"]["useRandomSeeds"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 2)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "teospecvd")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "teospecvd-plugin-test")

    def test_material_map_names_the_new_sio2_level_set(self) -> None:
        project = teospecvd_project(2)
        candidate = plane_domain(2)
        outcome = self._run(project, candidate)

        sio2_id = int(ps.Material.SiO2)
        si_id = int(ps.Material.Si)
        materials = outcome.state.materials
        self.assertIn(sio2_id, materials.order)
        self.assertIn(si_id, materials.order)
        self.assertEqual(materials.name_for(sio2_id), DEPOSIT_MATERIAL)
        self.assertEqual(materials.id_for(DEPOSIT_MATERIAL), sio2_id)
        self.assertEqual(materials.name_for(si_id), "Si")
        self.assertEqual(outcome.delta.materials, materials)
        self.assertEqual(outcome.metadata["materialMap"], list(materials.order))
        self.assertEqual(outcome.metadata["createdMaterialId"], sio2_id)

    def test_explicit_parameter_values_reach_the_native_model(self) -> None:
        project = teospecvd_project(2, duration=0.5, native_parameters=EXPLICIT_PARAMETERS)
        params = parse_step(project, project["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), EXPLICIT_PARAMETERS)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        self.assertEqual(outcome.metadata["parameters"], EXPLICIT_PARAMETERS)
        self.assertEqual(outcome.metadata["parameters"], params.flat_values())

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = teospecvd_project(2, duration=0.5)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity_json()["engineKernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = teospecvd_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.2)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self.assertGreater(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertIn(int(ps.Material.SiO2), outcome.delta.materials.order)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.teospecvd\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'worker' or name.startswith('worker.')\n"
            "    or name == 'backend' or name.startswith('backend.')\n"
            "    or name == 'viennaps' or name.startswith('viennaps.')\n"
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
