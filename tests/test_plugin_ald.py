"""Tests for the native v2 single-particle ALD plugin.

Every execution test builds a small 2D/3D Si plane, registers the
``ald``/``native`` plugin in a **local**
:class:`~process_models.v2.registry.ProcessRegistry`, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` holding the live
``viennaps`` module. The shared plugin registry, ``load_registry()``,
``run_v2_step()`` and ``plugins.lock`` are never consulted: adding an in-tree
plugin makes the checked-in lock stale until it is regenerated, which is out of
scope here.

The result is compared with a direct ``ps.SingleParticleALD`` + ``ps.Process``
reference through :func:`worker.process_models_v2.compare_domains`. That
reference mirrors ``worker/specialized.py::build_specialized_model`` and the
stage-8 direct-reference deposit path: duplicate the top level set as the
step's ``depositMaterial`` first, then set ``SingleParticleALDParams`` and the
``AtomicLayerProcessParameters`` (including ``numCycles``).

The model uses the stochastic CPU_DISK ray flux. Both the reference and the
plugin pin ``RayTracingParameters.useRandomSeeds=False`` with the same seed
(the plugin through ``configure_rays(ctx.seed)``), so the comparison is exact
without changing the plugin. Only CPU profiles are exercised: no test here
uses a GPU.
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
from plugins.ald import ALDNativeVariant, ALDParams, ALDProcessSpec, parse_step, register
from plugins.ald.params import ATOMIC_FIELDS, FLAT_FIELDS
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import configure_advection, configure_rays, material_handle
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-ald"
PLUGIN_TOML = ROOT / "plugins" / "ald" / "plugin.toml"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828
ENGINE_FINGERPRINT = "test-ald"

#: ``backend.specialized_models`` defaults for the ``ald`` catalog row.
CATALOG_DEFAULTS: dict[str, Any] = {
    "stickingProbability": 0.5,
    "gasMeanFreePath": 1.0,
    "growthPerCycle": 0.02,
    "incomingFlux": 1.0,
    "evaporationFlux": 0.1,
    "s0": 1.0,
    "coverageDiffusionCoefficient": 0.0,
    "pulseTime": 0.02,
    "purgePulseTime": 0.0,
    "coverageTimeStep": 0.01,
    "purgeTimeStep": 1.0,
    "numCycles": 1,
}

EXPLICIT_PARAMETERS: dict[str, Any] = {
    "stickingProbability": 0.8,
    "gasMeanFreePath": 0.5,
    "growthPerCycle": 0.05,
    "incomingFlux": 2.0,
    "evaporationFlux": 0.0,
    "s0": 0.9,
    "coverageDiffusionCoefficient": 0.0,
    "pulseTime": 0.05,
    "purgePulseTime": 0.01,
    "coverageTimeStep": 0.02,
    "purgeTimeStep": 0.5,
    "numCycles": 3,
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


def ald_step(
    *,
    duration: Any = 0.5,
    native_parameters: dict[str, Any] | None = None,
    deposit_material: Any = "Al2O3",
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    return {
        "id": "step_ald",
        "name": "ALD",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "ald",
        "operation": "deposit",
        "depositMaterial": deposit_material,
        "duration": duration,
        "nativeParameters": parameters,
    }


def ald_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    native_parameters: dict[str, Any] | None = None,
    duration: Any = 0.5,
    deposit_material: Any = "Al2O3",
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"ald plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [ald_step(duration=duration, native_parameters=native_parameters, deposit_material=deposit_material)],
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


def _reference(
    ps_module: Any,
    project: dict[str, Any],
    domain: Any,
    *,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the ALD deposit path directly, with no worker or plugin code.

    The top level set is duplicated as the step's ``depositMaterial`` before
    ``ps.Process`` is built, because ``SingleParticleALD`` grows that new level
    set. The parameter objects are filled exactly like
    ``worker/specialized.py`` does it, including ``numCycles``.
    """
    step = project["steps"][0]
    values = step["nativeParameters"]
    domain.duplicateTopLevelSet(material_handle(ps_module, step["depositMaterial"]))
    native_parameters = ps_module.SingleParticleALDParams()
    for name in FLAT_FIELDS:
        setattr(native_parameters, name, float(values[name]))
    model = ps_module.SingleParticleALD(native_parameters)
    atomic = ps_module.AtomicLayerProcessParameters()
    for name in ATOMIC_FIELDS:
        setattr(atomic, name, float(values[name]))
    atomic.numCycles = int(values["numCycles"])
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    process.setParameters(atomic)
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


class ALDPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.ps = ps
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
        self.assertEqual(manifest["id"], "ald")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.ald:register")

        self.assertEqual(ALDProcessSpec.id, "ald")
        self.assertEqual(ALDProcessSpec.version, "1.0.0")
        self.assertEqual(ALDProcessSpec.params_model, ALDParams)
        self.assertEqual(ALDProcessSpec.dimensions, (2, 3))
        self.assertEqual(ALDProcessSpec.operations, ("deposit",))
        self.assertEqual(ALDProcessSpec.default_variant, "native")
        self.assertIs(ALDProcessSpec.parse_step, parse_step)

        variant = ALDNativeVariant()
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
            {("cpu", "CPU_DISK"), ("cpu", "CPU_TRIANGLE"), ("gpu", "GPU_DISK")},
        )
        # This slice is CPU-tested and the GPU_DISK evidence is for that kernel
        # only; no GPU_TRIANGLE profile is advertised.
        self.assertNotIn(("gpu", "GPU_TRIANGLE"), profiles)
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "ald")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("ald", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("ald"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("ald")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        declared = [name for name in ALDParams.model_fields if name not in ("duration", "depositMaterial")]
        self.assertEqual(declared, list(CATALOG_DEFAULTS))

        params = ALDParams(duration=1.0, depositMaterial="Al2O3")
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                self.assertEqual(ALDParams.model_fields[name].default, fields[name]["default"])

        # Probability fields are bounded to [0, 1]; the catalog uses the
        # generic numeric ceiling for ``s0``, which this model tightens because
        # it is a sticking coefficient.
        for name in ("stickingProbability", "s0"):
            metadata = ALDParams.model_fields[name].metadata
            self.assertEqual(next(meta.ge for meta in metadata if hasattr(meta, "ge")), fields[name]["minimum"])
            self.assertEqual(next(meta.le for meta in metadata if hasattr(meta, "le")), 1.0)
        # ``gasMeanFreePath`` keeps its -1 sentinel floor.
        self.assertEqual(
            next(meta.ge for meta in ALDParams.model_fields["gasMeanFreePath"].metadata if hasattr(meta, "ge")),
            -1.0,
        )
        num_cycles = ALDParams.model_fields["numCycles"]
        self.assertEqual(next(meta.ge for meta in num_cycles.metadata if hasattr(meta, "ge")), 1)
        self.assertEqual(next(meta.le for meta in num_cycles.metadata if hasattr(meta, "le")), 10_000)
        self.assertIs(num_cycles.annotation, int)

        self.assertEqual(params.flat_values(), {name: float(CATALOG_DEFAULTS[name]) for name in FLAT_FIELDS})
        self.assertEqual(params.flat_arguments(), tuple(float(CATALOG_DEFAULTS[name]) for name in FLAT_FIELDS))
        self.assertEqual(
            params.atomic_values(), {**{name: CATALOG_DEFAULTS[name] for name in ATOMIC_FIELDS}, "numCycles": 1}
        )
        self.assertIsInstance(params.atomic_values()["numCycles"], int)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"stickingProbability": -0.1},
            {"stickingProbability": 1.1},
            {"stickingProbability": float("inf")},
            {"s0": -0.1},
            {"s0": 1.1},
            {"gasMeanFreePath": -1.1},
            {"growthPerCycle": -0.1},
            {"growthPerCycle": float("inf")},
            {"incomingFlux": -0.1},
            {"evaporationFlux": -0.1},
            {"coverageDiffusionCoefficient": -0.1},
            {"pulseTime": -0.1},
            {"purgePulseTime": -0.1},
            {"coverageTimeStep": -0.1},
            {"purgeTimeStep": -0.1},
            {"numCycles": 0},
            {"numCycles": 10_001},
            {"numCycles": 1.5},
            {"numCycles": "2"},
            {"numCycles": True},
            {"depositMaterial": ""},
            {"surfaceDiffusion": {"enabled": True}},
            {"unexpected": 1},
            {"growthPerCycle": "0.02"},
            {"growthPerCycle": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    ALDParams(**{"duration": 1.0, "depositMaterial": "Al2O3", **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = ald_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, ALDParams)
        assert params is not None
        self.assertEqual(params.duration, 0.5)
        self.assertEqual(params.depositMaterial, "Al2O3")
        self.assertEqual(params.flat_values(), {name: float(CATALOG_DEFAULTS[name]) for name in FLAT_FIELDS})
        self.assertEqual(params.numCycles, 1)

        explicit = ald_project(native_parameters=EXPLICIT_PARAMETERS)
        params = parse_step(explicit, explicit["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.flat_values(), {name: float(EXPLICIT_PARAMETERS[name]) for name in FLAT_FIELDS})
        self.assertEqual(
            params.atomic_values(),
            {name: float(EXPLICIT_PARAMETERS[name]) for name in ATOMIC_FIELDS} | {"numCycles": 3},
        )
        self.assertIsInstance(params.numCycles, int)

        sparse = ald_project()
        sparse["steps"][0]["nativeParameters"] = {"growthPerCycle": 0.2, "numCycles": 4.0}
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.growthPerCycle, 0.2)
        self.assertEqual(params.stickingProbability, 0.5)
        self.assertEqual(params.numCycles, 4)
        self.assertIsInstance(params.numCycles, int)

        claimed = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertIs(claimed[0], ALDProcessSpec)
        self.assertIsInstance(claimed[1], ALDParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = ald_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"precursors": {"rate": 0.1}}
        top_level_advanced = mutated(advanced={"precursors": {"rate": 0.1}})
        unknown = mutated()
        unknown["nativeParameters"]["surfaceDiffusionCoefficient"] = 0.1
        other_model = mutated(nativeModel="teosDeposition")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="etch")

        for label, candidate in (
            ("advanced", advanced),
            ("top-level advanced", top_level_advanced),
            ("unknown", unknown),
            ("teosDeposition", other_model),
            ("isotropic", not_specialized),
            ("etch", other_operation),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        unsupported_dimension = ald_project(2)
        unsupported_dimension["dimension"] = 4
        self.assertIsNone(parse_step(unsupported_dimension, unsupported_dimension["steps"][0]))

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("zero duration", mutated(duration=0.0)),
            ("negative duration", mutated(duration=-1.0)),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("nan duration", mutated(duration=float("nan"))),
            ("missing material", {k: v for k, v in step.items() if k != "depositMaterial"}),
            ("null material", mutated(depositMaterial=None)),
            ("empty material", mutated(depositMaterial="")),
            ("blank material", mutated(depositMaterial="   ")),
            ("non-string material", mutated(depositMaterial=7)),
            (
                "negative sticking",
                {**step, "nativeParameters": {**step["nativeParameters"], "stickingProbability": -0.1}},
            ),
            (
                "sticking above one",
                {**step, "nativeParameters": {**step["nativeParameters"], "stickingProbability": 1.1}},
            ),
            ("s0 above one", {**step, "nativeParameters": {**step["nativeParameters"], "s0": 1.1}}),
            (
                "mean free path below -1",
                {**step, "nativeParameters": {**step["nativeParameters"], "gasMeanFreePath": -1.1}},
            ),
            ("negative growth", {**step, "nativeParameters": {**step["nativeParameters"], "growthPerCycle": -0.1}}),
            ("negative flux", {**step, "nativeParameters": {**step["nativeParameters"], "incomingFlux": -0.1}}),
            ("negative time", {**step, "nativeParameters": {**step["nativeParameters"], "pulseTime": -0.1}}),
            ("zero cycles", {**step, "nativeParameters": {**step["nativeParameters"], "numCycles": 0}}),
            ("too many cycles", {**step, "nativeParameters": {**step["nativeParameters"], "numCycles": 10_001}}),
            ("fractional cycles", {**step, "nativeParameters": {**step["nativeParameters"], "numCycles": 1.5}}),
            ("string cycles", {**step, "nativeParameters": {**step["nativeParameters"], "numCycles": "2"}}),
            ("bool cycles", {**step, "nativeParameters": {**step["nativeParameters"], "numCycles": True}}),
            ("bool rate", {**step, "nativeParameters": {**step["nativeParameters"], "growthPerCycle": True}}),
            ("nan rate", {**step, "nativeParameters": {**step["nativeParameters"], "incomingFlux": float("nan")}}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_surface_diffusion_is_deferred_to_the_worker_path(self) -> None:
        # The 4.7.0 native stencil can hang, so this slice must not claim a
        # step that carries the object: the old worker path owns the gate.
        project = ald_project(native_parameters={"coverageDiffusionCoefficient": 5.0})
        step = project["steps"][0]
        self.assertIsInstance(parse_step(project, step), ALDParams)

        for label, surface_diffusion in (
            ("enabled", {"enabled": True, "radius": 0.0}),
            ("disabled", {"enabled": False}),
        ):
            with self.subTest(surface_diffusion=label):
                deferred = {**step, "surfaceDiffusion": surface_diffusion}
                self.assertIsNone(parse_step(project, deferred))
                self.assertIsNone(self.registry.resolve_step(project, deferred))

        # An explicit null is not an object and keeps the fast path.
        self.assertIsInstance(parse_step(project, {**step, "surfaceDiffusion": None}), ALDParams)

    def test_validate_returns_no_issues(self) -> None:
        variant = ALDNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=ald_project(),
        )
        self.assertEqual(variant.validate(ctx, ALDParams(duration=1.0, depositMaterial="Al2O3")), [])

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
                process_id="ald",
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

    def _assert_material_name(self, materials: MaterialState, name: str) -> int:
        material_id = int(getattr(ps.Material, name))
        self.assertEqual(materials.name_for(material_id), name)
        self.assertEqual(materials.id_for(name), material_id)
        self.assertEqual(materials.name_for(int(ps.Material.Si)), "Si")
        return material_id

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = ald_project(2, duration=0.5)
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
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.Al2O3)))
        self._assert_material_name(delta.materials, "Al2O3")
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.SingleParticleALD")
        self.assertEqual(metadata["nativeConstructor"], "SingleParticleALD(SingleParticleALDParams)")
        self.assertEqual(metadata["nativeParameterClass"], "SingleParticleALDParams")
        self.assertEqual(metadata["atomicLayerParameterClass"], "AtomicLayerProcessParameters")
        self.assertEqual(metadata["operation"], "deposit")
        self.assertEqual(metadata["depositMaterial"], "Al2O3")
        self.assertTrue(metadata["duplicateTopLevelSet"])
        self.assertEqual(metadata["parameters"], {name: float(CATALOG_DEFAULTS[name]) for name in FLAT_FIELDS})
        self.assertEqual(
            metadata["atomicLayerParameters"],
            {name: float(CATALOG_DEFAULTS[name]) for name in ATOMIC_FIELDS} | {"numCycles": 1},
        )
        self.assertEqual(metadata["numCycles"], 1)
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
        self.assertEqual(identity["processId"], "ald")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = ald_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.2)
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
        self._assert_material_name(outcome.delta.materials, "Al2O3")
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_num_cycles_are_applied_and_recorded(self) -> None:
        project = ald_project(2, native_parameters={"numCycles": 3}, duration=0.5)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        baseline = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["numCycles"], 3)
        self.assertIsInstance(outcome.metadata["numCycles"], int)
        self.assertEqual(outcome.metadata["atomicLayerParameters"]["numCycles"], 3)

        # A single cycle is the catalog default; three cycles must grow more.
        single = self._run(ald_project(2, duration=0.5), baseline)
        self.assertEqual(single.metadata["numCycles"], 1)
        self.assertGreater(_top(new_domain, 2), _top(single.state.geometry.handle, 2))

    def test_explicit_parameter_values_are_applied_and_recorded(self) -> None:
        project = ald_project(2, native_parameters=EXPLICIT_PARAMETERS, duration=0.3)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(
            outcome.metadata["parameters"],
            {name: float(EXPLICIT_PARAMETERS[name]) for name in FLAT_FIELDS},
        )
        self.assertEqual(
            outcome.metadata["atomicLayerParameters"],
            {name: float(EXPLICIT_PARAMETERS[name]) for name in ATOMIC_FIELDS} | {"numCycles": 3},
        )
        self.assertEqual(outcome.metadata["duration"], 0.3)
        self._assert_material_name(outcome.delta.materials, "Al2O3")

    def test_material_map_names_the_selectable_deposit_material(self) -> None:
        project = ald_project(2, deposit_material="TiO2", duration=0.3)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        tio2 = self._assert_material_name(outcome.delta.materials, "TiO2")
        order = material_order(new_domain)
        self.assertEqual(order, (int(ps.Material.Si), tio2))
        self.assertEqual(outcome.metadata["depositMaterial"], "TiO2")
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = ald_project(2, duration=0.2)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = ald_project(2)
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
                process_id="ald",
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
            "import plugins.ald\n"
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
