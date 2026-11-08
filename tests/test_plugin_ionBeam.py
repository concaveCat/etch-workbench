"""Tests for the native v2 ion-beam etch (IBE) plugin.

Each execution test builds one small 2D/3D level-set domain, registers the
``ionBeam``/``native`` plugin in a **local** :class:`ProcessRegistry`, and runs
it through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping the live
``viennaps`` module. The shared plugin registry and ``plugins.lock`` are never
consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.IBEParameters`` +
``ps.IonBeamEtching`` reference through
:func:`worker.process_models_v2.compare_domains`.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. No server or worker process is involved; the v2 state store writes
into ``var/test-plugin-ionBeam/``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import model_spec
from plugins.ionBeam import (
    IonBeamNativeVariant,
    IonBeamParams,
    IonBeamProcessSpec,
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
TEST_ROOT = ROOT / "var" / "test-plugin-ionBeam"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828

#: ``backend.specialized_models.ibe_fields()`` defaults for ``ionBeam``.
CATALOG_DEFAULTS: dict[str, Any] = {
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
    "materialPlaneWaferRateByMaterial": {},
}

#: Attributes the worker assigns before constructing ``IonBeamEtching``.
NATIVE_SCALARS = tuple(name for name in CATALOG_DEFAULTS if not name.startswith(("materialPlane", "cos4Yield")))


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


def ion_beam_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_ionBeam",
        "name": "IBE",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "ionBeam",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def ion_beam_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    geometry: dict[str, Any] | None = None,
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
        "name": f"ionBeam plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [ion_beam_step(duration=duration, native_parameters=native_parameters)],
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
    installed binding, so two CPU runs draw different rays and cannot be
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
    """Build the IBEParameters/IonBeamEtching path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    parameters = ps_module.IBEParameters()
    for name in NATIVE_SCALARS:
        setattr(parameters, name, float(values[name]))
    for index in range(1, 5):
        setattr(parameters.cos4Yield, f"a{index}", float(values[f"cos4YieldA{index}"]))
    for name, value in sorted(values["materialPlaneWaferRateByMaterial"].items()):
        parameters.materialPlaneWaferRate.set(material_handle(ps_module, name), float(value))
    masks = [material_handle(ps_module, name) for name in sorted(geometry_mask_material_names(project["geometry"]))]
    model = ps_module.IonBeamEtching(parameters, masks)
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
    process.apply()
    return domain


class IonBeamPluginTest(unittest.TestCase):
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
        manifest = tomllib.loads((ROOT / "plugins" / "ionBeam" / "plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest,
            {
                "api": 1,
                "id": "ionBeam",
                "version": "1.0.0",
                "entrypoint": "plugins.ionBeam:register",
                "description": "Native ion-beam etch (IBE)",
            },
        )

        self.assertEqual(IonBeamProcessSpec.id, "ionBeam")
        self.assertEqual(IonBeamProcessSpec.version, "1.0.0")
        self.assertEqual(IonBeamProcessSpec.params_model, IonBeamParams)
        self.assertEqual(IonBeamProcessSpec.dimensions, (2, 3))
        self.assertEqual(IonBeamProcessSpec.operations, ("etch",))
        self.assertEqual(IonBeamProcessSpec.default_variant, "native")
        self.assertIs(IonBeamProcessSpec.parse_step, parse_step)

        variant = IonBeamNativeVariant()
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
            (("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE"), ("gpu", "GPU_DISK")),
        )
        # GPU_TRIANGLE has no ionBeam evidence and must not be declared.
        self.assertNotIn("GPU_TRIANGLE", {profile.kernel for profile in variant.engine.profiles})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "ionBeam")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("ionBeam", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("ionBeam")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(list(CATALOG_DEFAULTS), list(IonBeamParams.model_fields)[1:])
        self.assertEqual(list(IonBeamParams.model_fields)[-1], "materialPlaneWaferRateByMaterial")

        params = IonBeamParams(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                model_field = IonBeamParams.model_fields[name]
                if name == "materialPlaneWaferRateByMaterial":
                    self.assertEqual(getattr(params, name), {})
                    self.assertEqual(model_field.default_factory(), {})
                    continue
                self.assertEqual(getattr(params, name), default)
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertEqual(
            params.flat_values(), {k: v for k, v in CATALOG_DEFAULTS.items() if k != "materialPlaneWaferRateByMaterial"}
        )
        self.assertEqual(params.material_rates(), {})
        self.assertEqual(params.native_scalar_values(), {name: CATALOG_DEFAULTS[name] for name in NATIVE_SCALARS})
        self.assertEqual(params.cos4_yield_values(), {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0})
        self.assertEqual(params.metadata_values(), CATALOG_DEFAULTS)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"meanEnergy": -0.1},
            {"sigmaEnergy": float("inf")},
            {"thresholdEnergy": 100.0},
            {"thetaRMin": 180.5},
            {"tiltAngle": -180.5},
            {"tiltAngle": 180.5},
            {"planeWaferRate": -1.0},
            {"materialPlaneWaferRateByMaterial": {"Si": -0.1}},
            {"materialPlaneWaferRateByMaterial": {"Si": float("inf")}},
            {"materialPlaneWaferRateByMaterial": {"": 0.0}},
            {"materialPlaneWaferRateByMaterial": [("Si", 0.0)]},
            {"unexpected": 1},
            {"meanEnergy": "100.0"},
            {"meanEnergy": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    IonBeamParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = ion_beam_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, IonBeamParams)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.metadata_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.material_rates(), {})

        with_table = ion_beam_project(
            native_parameters={
                "materialPlaneWaferRateByMaterial": {"Si": 0.05, "Mask": 0.0},
                "meanEnergy": 150.0,
                "thresholdEnergy": 25.0,
            }
        )
        params = parse_step(with_table, with_table["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.meanEnergy, 150.0)
        self.assertEqual(params.thresholdEnergy, 25.0)
        self.assertEqual(params.material_rates(), {"Mask": 0.0, "Si": 0.05})

        sparse = ion_beam_project()
        sparse["steps"][0]["nativeParameters"] = {"meanEnergy": 300, "materialPlaneWaferRateByMaterial": {"Si": 1}}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.meanEnergy, 300.0)
        self.assertEqual(params.thresholdEnergy, 20.0)
        self.assertEqual(params.material_rates(), {"Si": 1.0})

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], IonBeamProcessSpec)
        self.assertIsInstance(resolved[1], IonBeamParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = ion_beam_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        unknown = mutated()
        unknown["nativeParameters"]["cageAngle"] = 30.0
        other_model = mutated(nativeModel="sf6o2")
        not_specialized = mutated(model="isotropic")

        for label, candidate in (
            ("advanced", advanced),
            ("unknown", unknown),
            ("sf6o2", other_model),
            ("isotropic", not_specialized),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        without_duration = {k: v for k, v in step.items() if k != "duration"}
        invalid = (
            ("missing duration", without_duration),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("nan duration", mutated(duration=float("nan"))),
            ("bool energy", {**step, "nativeParameters": {**step["nativeParameters"], "meanEnergy": True}}),
            ("nan energy", {**step, "nativeParameters": {**step["nativeParameters"], "meanEnergy": float("nan")}}),
            ("angle", {**step, "nativeParameters": {**step["nativeParameters"], "thetaRMax": 200.0}}),
            (
                "energy order",
                {**step, "nativeParameters": {**step["nativeParameters"], "thresholdEnergy": 100.0}},
            ),
            (
                "table type",
                {**step, "nativeParameters": {**step["nativeParameters"], "materialPlaneWaferRateByMaterial": [1]}},
            ),
            (
                "table value",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "materialPlaneWaferRateByMaterial": {"Si": "0.1"},
                    },
                },
            ),
            (
                "table negative",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "materialPlaneWaferRateByMaterial": {"Si": -0.1},
                    },
                },
            ),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_energy_order_is_rejected_before_native_construction(self) -> None:
        with self.assertRaises(ValueError) as raised:
            IonBeamParams(duration=1.0, meanEnergy=100.0, thresholdEnergy=100.0)
        self.assertIn("less than meanEnergy", str(raised.exception))

        project = ion_beam_project()
        step = project["steps"][0]
        for threshold in (100.0, 200.0):
            candidate = {
                **step,
                "nativeParameters": {**step["nativeParameters"], "thresholdEnergy": threshold},
            }
            with self.subTest(threshold=threshold):
                with self.assertRaises(ValueError) as raised:
                    parse_step(project, candidate)
                self.assertIn("less than meanEnergy", str(raised.exception))

        # The variant repeats the rule for callers that bypass the Pydantic
        # model entirely (for example a replay object built by hand).
        invalid = IonBeamParams.model_construct(duration=1.0, meanEnergy=100.0, thresholdEnergy=200.0)
        issues = IonBeamNativeVariant().validate(
            ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project),
            invalid,
        )
        self.assertEqual([issue.path for issue in issues], ["thresholdEnergy"])

    def test_validate_returns_no_issues(self) -> None:
        variant = IonBeamNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=ion_beam_project(),
        )
        self.assertEqual(variant.validate(ctx, IonBeamParams(duration=1.0)), [])

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
                    fingerprint="ionBeam-plugin-test",
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
        project = ion_beam_project(
            2,
            native_parameters={"materialPlaneWaferRateByMaterial": {"Si": 0.05}},
        )
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
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
        self.assertEqual(metadata["nativeClass"], "ps.IonBeamEtching")
        self.assertEqual(metadata["nativeConstructor"], "IBEParameters + mask material list")
        self.assertEqual(
            metadata["parameters"],
            {name: value for name, value in CATALOG_DEFAULTS.items() if name != "materialPlaneWaferRateByMaterial"},
        )
        self.assertEqual(metadata["materialPlaneWaferRateByMaterial"], {"Si": 0.05})
        self.assertEqual(metadata["cos4Yield"], {"a1": 0.0, "a2": 0.0, "a3": 0.0, "a4": 0.0})
        self.assertEqual(metadata["duration"], 0.5)
        self.assertEqual(metadata["maskMaterials"], [])
        self.assertTrue(metadata["maskMaterialPlaneWaferRateForcedZero"])
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
        self.assertEqual(identity["processId"], "ionBeam")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "ionBeam-plugin-test")

    def test_masked_trench_matches_the_native_reference(self) -> None:
        project = ion_beam_project(
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
            native_parameters={"materialPlaneWaferRateByMaterial": {"Si": 0.04}},
        )
        self.assertEqual(geometry_mask_material_names(project["geometry"]), ["Mask"])
        reference = trench_domain()
        candidate = trench_domain()

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        order = material_order(new_domain)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self.assertEqual(outcome.metadata["maskMaterials"], ["Mask"])
        self.assertEqual(outcome.metadata["materialPlaneWaferRateByMaterial"], {"Si": 0.04})

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = ion_beam_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.1)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference)
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

    def test_cpu_triangle_matches_direct_reference_on_a_small_2d_plane(self) -> None:
        project = ion_beam_project(
            2,
            grid_delta=0.4,
            x_extent=2.0,
            duration=0.2,
            native_parameters={"materialPlaneWaferRateByMaterial": {"Si": 0.05}},
        )
        reference = plane_domain(2, grid_delta=0.4, x_extent=2.0)
        candidate = plane_domain(2, grid_delta=0.4, x_extent=2.0)

        _reference(self.ps, project, reference, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = ion_beam_project(2)
        del project["solver"]
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        context = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project, step=step, seed=RAY_SEED)
        variant = IonBeamNativeVariant()
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
            "import plugins.ionBeam\n"
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
