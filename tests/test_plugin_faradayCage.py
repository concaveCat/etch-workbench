"""Tests for the native v2 Faraday-cage ion-beam-etch plugin.

Each execution test builds one small 2D/3D periodic level-set domain, registers
the ``faradayCage``/``native`` plugin in a **local** :class:`ProcessRegistry`,
and runs it through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping
the live ``viennaps`` module. The shared plugin registry and ``plugins.lock``
are never consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.FaradayCageParameters`` +
``ps.FaradayCageEtching`` + ``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`.

``FaradayCageEtching`` builds a periodic source, so every domain here pins both
lateral boundaries to periodic. The installed ``RayTracingParameters`` default
is ``useRandomSeeds=True``, so CPU_DISK flux noise would differ between two
otherwise identical runs. Both the reference and the plugin run under
:class:`_SeededModule`, a test-only module proxy that pins the ray parameters on
every ``Process`` it constructs. The plugin itself is unchanged: it still uses
whatever ``ps`` the engine lease provides. No server or worker process is
involved; the v2 state store writes into ``var/test-plugin-faradayCage/``.

ViennaPS 4.7.0 returns no etch for the 2D kernel while still moving the level
set's node count; the 2D cases therefore assert exact parity (and the 3D cases
also assert that the exposed film actually etches), so the module is meaningful
on both 4.6.2 and 4.7.0.
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
from plugins.faradayCage import (
    FaradayCageNativeVariant,
    FaradayCageParams,
    FaradayCageProcessSpec,
    parse_step,
    register,
)
from plugins.faradayCage.params import (
    COS4_FIELDS,
    IBE_SCALAR_FIELDS,
    MATERIAL_RATE_FIELD,
    SCALAR_FIELDS,
)
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import (
    configure_advection,
    configure_rays,
    geometry_mask_material_names,
    material_handle,
)
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-faradayCage"

GRID_DELTA = 0.2
X_EXTENT = 2.0
GRID_DELTA_3D = 0.4
X_EXTENT_3D = 2.0
DURATION = 0.5
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828

#: ``backend.specialized_models.CATALOG`` defaults for ``faradayCage``.
CATALOG_DEFAULTS = {
    "cageAngle": 0.0,
    "meanEnergy": 100.0,
    "sigmaEnergy": 10.0,
    "thresholdEnergy": 20.0,
    "thetaRMin": 70.0,
    "thetaRMax": 90.0,
    "minAngle": 85.0,
    "inflectAngle": 89.0,
    "exponent": 100.0,
    "n_l": 10.0,
    "tiltAngle": 0.0,
    "planeWaferRate": 0.1,
    "redepositionRate": 0.0,
    "redepositionThreshold": 0.1,
    "cos4YieldA1": 0.0,
    "cos4YieldA2": 0.0,
    "cos4YieldA3": 0.0,
    "cos4YieldA4": 0.0,
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


def faraday_step(
    *,
    duration: Any = DURATION,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters[MATERIAL_RATE_FIELD] = {}
    parameters.update(native_parameters or {})
    return {
        "id": "step_faradayCage",
        "name": "Faraday cage",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "faradayCage",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def faraday_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    geometry: dict[str, Any] | None = None,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = DURATION,
    boundary: str = "periodic",
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = boundary
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = boundary
    return {
        "schemaVersion": 7,
        "name": f"faradayCage plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [faraday_step(duration=duration, native_parameters=native_parameters)],
        "solver": _solver(),
    }


def plane_domain(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
) -> Any:
    ps.setDimension(dimension)
    boundary = ps.BoundaryType.PERIODIC_BOUNDARY
    if dimension == 2:
        domain = ps.Domain(gridDelta=grid_delta, xExtent=x_extent, boundary=boundary)
    else:
        domain = ps.Domain(gridDelta=grid_delta, xExtent=x_extent, yExtent=x_extent, boundary=boundary)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def trench_domain() -> Any:
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=4.0,
        boundary=ps.BoundaryType.PERIODIC_BOUNDARY,
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


def _reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Build the FaradayCage parameter-object path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    parameters = ps_module.FaradayCageParameters()
    for name in IBE_SCALAR_FIELDS:
        setattr(parameters.ibeParams, name, float(values[name]))
    for index, name in enumerate(COS4_FIELDS, start=1):
        setattr(parameters.ibeParams.cos4Yield, f"a{index}", float(values[name]))
    for name, value in sorted(values.get(MATERIAL_RATE_FIELD, {}).items()):
        parameters.ibeParams.materialPlaneWaferRate.set(
            material_handle(ps_module, name),
            float(value),
        )
    parameters.cageAngle = float(values["cageAngle"])
    mask_names = sorted(geometry_mask_material_names(project["geometry"]))
    model = ps_module.FaradayCageEtching(
        parameters,
        [material_handle(ps_module, name) for name in mask_names],
    )
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _advection_actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
    process.apply()
    return domain


class FaradayCagePluginTest(unittest.TestCase):
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
        self.assertEqual(FaradayCageProcessSpec.id, "faradayCage")
        self.assertEqual(FaradayCageProcessSpec.version, "1.0.0")
        self.assertEqual(FaradayCageProcessSpec.params_model, FaradayCageParams)
        self.assertEqual(FaradayCageProcessSpec.dimensions, (2, 3))
        self.assertEqual(FaradayCageProcessSpec.operations, ("etch",))
        self.assertEqual(FaradayCageProcessSpec.default_variant, "native")
        self.assertIs(FaradayCageProcessSpec.parse_step, parse_step)

        variant = FaradayCageNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertTrue(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertTrue(variant.capabilities.experimental)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertFalse(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        # CPU_DISK is the only profile: 2D AUTO/GPU hangs for part of the
        # cage/tilt matrix, and this plugin does not claim 3D GPU either.
        self.assertEqual(
            tuple((profile.backend, profile.kernel) for profile in variant.engine.profiles),
            (("cpu", "CPU_DISK"),),
        )
        kernels = {profile.kernel for profile in variant.engine.profiles}
        self.assertNotIn("CPU_TRIANGLE", kernels)
        self.assertNotIn("GPU_DISK", kernels)
        self.assertNotIn("GPU_TRIANGLE", kernels)
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "faradayCage")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("faradayCage", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("faradayCage")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(list(fields), [*SCALAR_FIELDS, MATERIAL_RATE_FIELD])
        self.assertEqual(
            list(FaradayCageParams.model_fields),
            ["duration", *SCALAR_FIELDS, MATERIAL_RATE_FIELD],
        )
        self.assertEqual(tuple(CATALOG_DEFAULTS), SCALAR_FIELDS)

        params = FaradayCageParams(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = FaradayCageParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertEqual(params.materialPlaneWaferRateByMaterial, {})
        self.assertEqual(params.scalar_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.ibe_values(), {name: CATALOG_DEFAULTS[name] for name in IBE_SCALAR_FIELDS})
        self.assertEqual(params.cos4_values(), {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0})

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"cageAngle": -1.0},
            {"cageAngle": 181.0},
            {"cageAngle": float("nan")},
            {"meanEnergy": -0.1},
            {"meanEnergy": 1_000_001.0},
            {"sigmaEnergy": float("inf")},
            {"thetaRMin": 181.0},
            {"thetaRMax": -1.0},
            {"minAngle": 181.0},
            {"inflectAngle": -1.0},
            {"tiltAngle": -181.0},
            {"tiltAngle": 181.0},
            {"tiltAngle": float("nan")},
            {"exponent": float("inf")},
            {"redepositionThreshold": -0.1},
            {"cos4YieldA1": -0.1},
            {"cos4YieldA4": 1_000_001.0},
            {MATERIAL_RATE_FIELD: {"Si": -0.1}},
            {MATERIAL_RATE_FIELD: {"Si": 1_000_001.0}},
            {MATERIAL_RATE_FIELD: {"Si": float("nan")}},
            {MATERIAL_RATE_FIELD: {"Si": True}},
            {MATERIAL_RATE_FIELD: {"Si": "0.1"}},
            {MATERIAL_RATE_FIELD: {"": 0.1}},
            {"unexpected": 1},
            {"cageAngle": "30.0"},
            {"cageAngle": True},
            # thresholdEnergy >= meanEnergy reverses the native flux sign.
            {"thresholdEnergy": 100.0},
            {"meanEnergy": 50.0, "thresholdEnergy": 50.0},
            {"meanEnergy": 10.0},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    FaradayCageParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_scalars_and_material_rates(self) -> None:
        project = faraday_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, FaradayCageParams)
        assert params is not None
        self.assertEqual(params.duration, DURATION)
        self.assertEqual(params.scalar_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.materialPlaneWaferRateByMaterial, {})

        explicit = faraday_project(
            native_parameters={
                "cageAngle": 30.0,
                "tiltAngle": 30.0,
                MATERIAL_RATE_FIELD: {"Si": 0.15},
            }
        )
        params = parse_step(explicit, explicit["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.cageAngle, 30.0)
        self.assertEqual(params.tiltAngle, 30.0)
        self.assertEqual(params.materialPlaneWaferRateByMaterial, {"Si": 0.15})
        self.assertEqual(params.meanEnergy, 100.0)

        sparse = faraday_project(native_parameters={})
        sparse["steps"][0]["nativeParameters"] = {"cageAngle": 45}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.cageAngle, 45.0)
        self.assertEqual(params.ibe_values(), {name: CATALOG_DEFAULTS[name] for name in IBE_SCALAR_FIELDS})

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], FaradayCageProcessSpec)
        self.assertIsInstance(resolved[1], FaradayCageParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = faraday_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        unknown = mutated()
        unknown["nativeParameters"]["ionFlux"] = 0.2
        other_model = mutated(nativeModel="ionBeam")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="deposit")

        for label, candidate in (
            ("advanced", advanced),
            ("unknown", unknown),
            ("ionBeam", other_model),
            ("isotropic", not_specialized),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))
        # Dispatch is ``model``/``nativeModel`` only, as specified. The project
        # validator rejects a deposit-labelled faradayCage step before dispatch.
        self.assertIsInstance(parse_step(project, other_operation), FaradayCageParams)

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("string duration", mutated(duration="1.0")),
            ("nan duration", mutated(duration=float("nan"))),
            ("bool cage angle", {**step, "nativeParameters": {**step["nativeParameters"], "cageAngle": True}}),
            ("string scalar", {**step, "nativeParameters": {**step["nativeParameters"], "meanEnergy": "100"}}),
            ("nan scalar", {**step, "nativeParameters": {**step["nativeParameters"], "sigmaEnergy": float("nan")}}),
            ("cage range", {**step, "nativeParameters": {**step["nativeParameters"], "cageAngle": 181.0}}),
            ("tilt range", {**step, "nativeParameters": {**step["nativeParameters"], "tiltAngle": -181.0}}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
            (
                "material table type",
                {**step, "nativeParameters": {**step["nativeParameters"], MATERIAL_RATE_FIELD: []}},
            ),
            (
                "material rate range",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        MATERIAL_RATE_FIELD: {"Si": -0.1},
                    },
                },
            ),
            (
                "material rate string",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        MATERIAL_RATE_FIELD: {"Si": "0.1"},
                    },
                },
            ),
            (
                "threshold equals mean",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "meanEnergy": 100.0,
                        "thresholdEnergy": 100.0,
                    },
                },
            ),
            (
                "threshold above mean",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "meanEnergy": 100.0,
                        "thresholdEnergy": 200.0,
                    },
                },
            ),
            (
                "mean below threshold",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "meanEnergy": 10.0,
                    },
                },
            ),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_threshold_energy_must_stay_below_mean_energy(self) -> None:
        for threshold in (100.0, 200.0):
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError):
                    FaradayCageParams(duration=1.0, meanEnergy=100.0, thresholdEnergy=threshold)
        # The near-boundary valid value is an ordinary parameter object.
        params = FaradayCageParams(duration=1.0, meanEnergy=100.0, thresholdEnergy=99.9)
        self.assertEqual(params.thresholdEnergy, 99.9)

    def test_validate_returns_no_issues(self) -> None:
        variant = FaradayCageNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=faraday_project(),
        )
        self.assertEqual(variant.validate(ctx, FaradayCageParams(duration=1.0)), [])

    # -- execution ---------------------------------------------------------

    def _run(
        self,
        project: dict[str, Any],
        domain: Any,
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
                        kernels=("CPU_DISK",),
                    ),
                    handle=self.ps,
                    fingerprint="faradayCage-plugin-test",
                )
            ]
        )
        store = StateStore(self.work_dir / "CPU_DISK" / step["id"])
        flow = FlowExecutor(self.registry, provider, store).run(
            state,
            [
                FlowStep(
                    step_id=step["id"],
                    process_id=spec.id,
                    variant_id=self.registry.default_variant_id(spec.id),
                    params=params,
                    context=context,
                    compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
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

    def test_two_dimensional_periodic_plane_matches_the_native_reference(self) -> None:
        project = faraday_project(2, native_parameters={MATERIAL_RATE_FIELD: {"Si": 0.15}})
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIsNot(new_domain, candidate)
        # The executor checkpoints the mutating variant: the input is untouched.
        self.assertTrue(compare_domains(plane_domain(2), candidate, 2)["matched"])

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
        self.assertEqual(metadata["nativeClass"], "ps.FaradayCageEtching")
        self.assertEqual(metadata["nativeParameterContainer"], "ps.FaradayCageParameters")
        self.assertEqual(metadata["cageAngle"], 0.0)
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["ibeParameters"], {name: CATALOG_DEFAULTS[name] for name in IBE_SCALAR_FIELDS})
        self.assertEqual(metadata["cos4Yield"], {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0})
        self.assertEqual(metadata["materialPlaneWaferRateByMaterial"], {"Si": 0.15})
        self.assertEqual(metadata["maskMaterials"], [])
        self.assertTrue(metadata["maskMaterialPlaneWaferRateForcedZero"])
        self.assertEqual(metadata["duration"], DURATION)
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["rayTracing"]["raysPerPoint"], RAYS_PER_POINT)
        self.assertEqual(metadata["rayTracing"]["rngSeed"], RAY_SEED)
        self.assertFalse(metadata["rayTracing"]["useRandomSeeds"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 1)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "faradayCage")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "faradayCage-plugin-test")

    def test_masked_two_dimensional_trench_matches_the_native_reference(self) -> None:
        project = faraday_project(
            2,
            x_extent=4.0,
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
        )
        reference = trench_domain()
        candidate = trench_domain()

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        order = material_order(new_domain)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["maskMaterials"], ["Mask"])
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_three_dimensional_periodic_plane_matches_the_native_reference(self) -> None:
        project = faraday_project(3, grid_delta=GRID_DELTA_3D, x_extent=X_EXTENT_3D)
        reference = plane_domain(3, grid_delta=GRID_DELTA_3D, x_extent=X_EXTENT_3D)
        candidate = plane_domain(3, grid_delta=GRID_DELTA_3D, x_extent=X_EXTENT_3D)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertLess(_top(new_domain, 3), before - 1e-9)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA_3D)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

    def test_three_dimensional_nonzero_cage_angle_and_tilt_match_the_reference(self) -> None:
        project = faraday_project(
            3,
            grid_delta=GRID_DELTA_3D,
            x_extent=X_EXTENT_3D,
            native_parameters={"cageAngle": 30.0, "tiltAngle": 30.0},
        )
        reference = plane_domain(3, grid_delta=GRID_DELTA_3D, x_extent=X_EXTENT_3D)
        candidate = plane_domain(3, grid_delta=GRID_DELTA_3D, x_extent=X_EXTENT_3D)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertLess(_top(new_domain, 3), before - 1e-9)
        self.assertEqual(outcome.metadata["cageAngle"], 30.0)
        self.assertEqual(outcome.metadata["ibeParameters"]["tiltAngle"], 30.0)
        self.assertEqual(outcome.metadata["parameters"]["cageAngle"], 30.0)
        self.assertEqual(outcome.metadata["parameters"]["tiltAngle"], 30.0)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = faraday_project(2)
        del project["solver"]
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        context = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project, step=step, seed=RAY_SEED)
        variant = FaradayCageNativeVariant()
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
            "import plugins.faradayCage\n"
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
