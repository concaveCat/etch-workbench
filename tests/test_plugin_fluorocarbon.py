"""Tests for the native v2 fluorocarbon plasma-etch plugin.

Each execution test builds one small 2D/3D level-set domain, registers the
``fluorocarbon``/``native`` plugin in a **local** :class:`ProcessRegistry`, and
runs it through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping the
live ``viennaps`` module. The shared plugin registry and ``plugins.lock`` are
never consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.FluorocarbonParameters`` +
``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`. The reference receives the
material table explicitly, so the default-fill test proves the plugin produced
the same native rows as a caller who wrote every row out.

The installed ``RayTracingParameters`` default is ``useRandomSeeds=True``, so
CPU_DISK flux noise differs between two otherwise identical runs. Both the
reference and the plugin run under :class:`_SeededModule`, a test-only module
proxy that pins the ray parameters on every ``Process`` it constructs. The
plugin itself is unchanged: it still uses whatever ``ps`` the engine lease
provides. No server or worker process is involved; the v2 state store writes
into ``var/test-plugin-fluorocarbon/``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from backend.specialized_models import FLUOROCARBON_MATERIAL_DEFAULTS, model_spec
from plugins.fluorocarbon import (
    FluorocarbonNativeVariant,
    FluorocarbonParams,
    FluorocarbonProcessSpec,
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
    ViennaPSMaterialRegistry,
    configure_advection,
    configure_rays,
    material_handle,
)
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-fluorocarbon"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
RAYS_PER_POINT = 32
RAY_SEED = 271828

#: ``model_spec("fluorocarbon")`` scalar defaults, in catalog field order.
CATALOG_DEFAULTS = {
    "ionFlux": 1.0,
    "etchantFlux": 1.0,
    "polyFlux": 0.2,
    "k_ie": 2.0,
    "k_ev": 2.0,
    "delta_p": 1.0,
    "temperature": 300.0,
    "meanEnergy": 100.0,
    "sigmaEnergy": 10.0,
    "exponent": 500.0,
    "n_l": 10.0,
    "inflectAngle": 1.55334303,
    "minAngle": 1.3962634,
    "etchStopDepth": -1_000_000.0,
}

MATERIAL_NAMES = ("Mask", "Polymer", "Si")


def _material_table(**overrides: dict[str, float]) -> dict[str, dict[str, float]]:
    table = {name: dict(FLUOROCARBON_MATERIAL_DEFAULTS) for name in MATERIAL_NAMES}
    table.update(overrides)
    return table


MATERIAL_TABLE = _material_table()


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


def fluorocarbon_step(
    *,
    duration: Any = 0.2,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = dict(CATALOG_DEFAULTS)
    parameters["materialParametersByMaterial"] = _material_table()
    parameters.update(native_parameters or {})
    return {
        "id": "step_fluorocarbon",
        "name": "Fluorocarbon",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "fluorocarbon",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def fluorocarbon_project(
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
        "name": f"fluorocarbon plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [fluorocarbon_step(duration=duration, native_parameters=native_parameters)],
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


def _material_names(domain: Any) -> dict[int, str]:
    registry = ViennaPSMaterialRegistry(ps)
    names: dict[int, str] = {}
    for material_id in material_order(domain):
        name = registry.name_for(material_id)
        if name is None:
            raise AssertionError(f"native material id {material_id} has no name")
        names[int(material_id)] = name
    return names


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
    table: dict[str, dict[str, float]],
    *,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the ``FluorocarbonParameters`` path directly, with no worker code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    parameters = ps_module.FluorocarbonParameters()
    for key in ("ionFlux", "etchantFlux", "polyFlux", "k_ie", "k_ev", "delta_p", "temperature", "etchStopDepth"):
        setattr(parameters, key, float(values[key]))
    ions = parameters.Ions
    for key in ("meanEnergy", "sigmaEnergy", "exponent", "n_l", "inflectAngle", "minAngle"):
        setattr(ions, key, float(values[key]))
    for name, row_values in sorted(table.items()):
        row = ps_module.FluorocarbonMaterialParameters()
        row.id = ps_module.Material.Polymer if name == "Polymer" else material_handle(ps_module, name)
        for key, value in row_values.items():
            setattr(row, key, float(value))
        parameters.addMaterial(row)
    model = ps_module.FluorocarbonEtching(parameters)
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps_module.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps_module, project, RAY_SEED)
    process.setParameters(rays)
    process.apply()
    return domain


class FluorocarbonPluginTest(unittest.TestCase):
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
        self.assertEqual(FluorocarbonProcessSpec.id, "fluorocarbon")
        self.assertEqual(FluorocarbonProcessSpec.version, "1.0.0")
        self.assertEqual(FluorocarbonProcessSpec.params_model, FluorocarbonParams)
        self.assertEqual(FluorocarbonProcessSpec.dimensions, (2, 3))
        self.assertEqual(FluorocarbonProcessSpec.operations, ("etch",))
        self.assertEqual(FluorocarbonProcessSpec.default_variant, "native")
        self.assertIs(FluorocarbonProcessSpec.parse_step, parse_step)

        variant = FluorocarbonNativeVariant()
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
        # CPU_TRIANGLE parity is covered below; GPU_DISK has archived evidence.
        # GPU_TRIANGLE has no evidence row for this model and must not be
        # claimed.
        self.assertNotIn("GPU_TRIANGLE", {profile.kernel for profile in variant.engine.profiles})
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "fluorocarbon")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("fluorocarbon", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("fluorocarbon")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"] if field["key"] != "materialParametersByMaterial"}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(list(CATALOG_DEFAULTS), list(FluorocarbonParams.model_fields)[1:-1])

        params = FluorocarbonParams(duration=1.0)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = FluorocarbonParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertEqual(dict(params.materialParametersByMaterial), {})
        self.assertEqual(params.scalar_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.material_values(), {})

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"ionFlux": -0.1},
            {"polyFlux": float("inf")},
            {"temperature": 1_000_001.0},
            {"k_ie": -1.0},
            {"etchStopDepth": -1_000_001.0},
            {"unexpected": 1},
            {"ionFlux": "1.0"},
            {"ionFlux": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    FluorocarbonParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_material_table_defaults_and_validation(self) -> None:
        spec = model_spec("fluorocarbon")
        assert spec is not None
        catalog_row = next(field for field in spec["fields"] if field["key"] == "materialParametersByMaterial")
        catalog_names = [
            row["name"] for row in json.loads((ROOT / "docs/materials-catalog.json").read_text())["materials"]
        ]

        from plugins.fluorocarbon.params import CATALOG_MATERIAL_NAMES, MATERIAL_FIELDS

        self.assertEqual(catalog_row["defaults"], FLUOROCARBON_MATERIAL_DEFAULTS)
        self.assertEqual(tuple(FLUOROCARBON_MATERIAL_DEFAULTS), MATERIAL_FIELDS)
        self.assertEqual(list(CATALOG_MATERIAL_NAMES), catalog_names)

        params = FluorocarbonParams(
            duration=1.0,
            materialParametersByMaterial=_material_table(),
        )
        self.assertEqual(params.material_values(), MATERIAL_TABLE)
        row = params.materialParametersByMaterial["Si"]
        self.assertEqual(row.values(), FLUOROCARBON_MATERIAL_DEFAULTS)
        for name, default in FLUOROCARBON_MATERIAL_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(row, name), default)
                model_field = row.__class__.model_fields[name]
                self.assertTrue(model_field.is_required())
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, catalog_row["minimum"])
                self.assertEqual(le, catalog_row["maximum"])

        row_type = type(row)
        for overrides in (
            {"A_ie": -1_000_001.0},
            {"density": 1_000_001.0},
            {"beta_e": float("nan")},
            {"beta_p": float("inf")},
            {"K": "0.002"},
            {"B_sp": True},
        ):
            with self.subTest(row_overrides=overrides):
                with self.assertRaises(ValidationError):
                    row_type(**{**FLUOROCARBON_MATERIAL_DEFAULTS, **overrides})

        with self.assertRaises(ValidationError):
            row_type(**{name: value for name, value in FLUOROCARBON_MATERIAL_DEFAULTS.items() if name != "density"})
        with self.assertRaises(ValidationError):
            row_type(**FLUOROCARBON_MATERIAL_DEFAULTS, unknown=1.0)

        for table in (
            {"Unobtainium": dict(FLUOROCARBON_MATERIAL_DEFAULTS)},
            {"": dict(FLUOROCARBON_MATERIAL_DEFAULTS)},
            {"Si": {**FLUOROCARBON_MATERIAL_DEFAULTS, "unknown": 1.0}},
            {"Si": {name: value for name, value in FLUOROCARBON_MATERIAL_DEFAULTS.items() if name != "K"}},
            {"Si": [1, 2, 3]},
        ):
            with self.subTest(table=table):
                with self.assertRaises(ValidationError):
                    FluorocarbonParams(duration=1.0, materialParametersByMaterial=table)

        with self.assertRaises(ValidationError):
            row.density = 3.0  # frozen

    def test_parse_step_maps_scalars_and_defaults(self) -> None:
        project = fluorocarbon_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, FluorocarbonParams)
        assert params is not None
        self.assertEqual(params.duration, 0.2)
        self.assertEqual(params.scalar_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.material_values(), MATERIAL_TABLE)

        sparse = fluorocarbon_project(
            native_parameters={
                "ionFlux": 2,
                "k_ie": 1.5,
                "materialParametersByMaterial": {"Si": dict(FLUOROCARBON_MATERIAL_DEFAULTS)},
            }
        )
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.ionFlux, 2.0)
        self.assertEqual(params.k_ie, 1.5)
        self.assertEqual(params.etchantFlux, 1.0)
        self.assertEqual(params.meanEnergy, 100.0)
        self.assertEqual(set(params.materialParametersByMaterial), {"Si"})

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], FluorocarbonProcessSpec)
        self.assertIsInstance(resolved[1], FluorocarbonParams)

    def test_parse_step_rejects_advanced_unknown_and_malformed(self) -> None:
        project = fluorocarbon_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {
                **step,
                "nativeParameters": {
                    **step["nativeParameters"],
                    "materialParametersByMaterial": _material_table(),
                },
            }
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"Ions": {"meanEnergy": 60.0}}
        unknown_key = mutated()
        unknown_key["nativeParameters"]["coverageTolerance"] = 0.01
        other_model = mutated(nativeModel="cf4o2")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="deposit")

        for label, candidate in (
            ("advanced", advanced),
            ("coverageTolerance", unknown_key),
            ("cf4o2", other_model),
            ("isotropic", not_specialized),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))
        # Dispatch is ``model``/``nativeModel`` only, as specified. The project
        # validator rejects a deposit-labelled fluorocarbon step before
        # dispatch, so this process still only ever runs as an etch.
        self.assertIsInstance(parse_step(project, other_operation), FluorocarbonParams)

        def table(**row_changes: Any) -> dict[str, Any]:
            rows = _material_table()
            rows["Si"] = {**rows["Si"], **row_changes}
            return mutated(nativeParameters={**step["nativeParameters"], "materialParametersByMaterial": rows})

        invalid = (
            ("missing duration", {key: value for key, value in step.items() if key != "duration"}),
            ("string duration", mutated(duration="1.0")),
            ("bool flux", mutated(nativeParameters={**step["nativeParameters"], "ionFlux": True})),
            ("nan flux", mutated(nativeParameters={**step["nativeParameters"], "ionFlux": float("nan")})),
            ("infinite ion energy", mutated(nativeParameters={**step["nativeParameters"], "meanEnergy": float("inf")})),
            (
                "parameters type",
                {**step, "nativeParameters": [1, 2, 3]},
            ),
            (
                "material table type",
                mutated(nativeParameters={**step["nativeParameters"], "materialParametersByMaterial": ["Si"]}),
            ),
            (
                "material row type",
                mutated(
                    nativeParameters={
                        **step["nativeParameters"],
                        "materialParametersByMaterial": {"Si": ["density"]},
                    }
                ),
            ),
            (
                "unknown material",
                mutated(
                    nativeParameters={
                        **step["nativeParameters"],
                        "materialParametersByMaterial": {"Unobtainium": dict(FLUOROCARBON_MATERIAL_DEFAULTS)},
                    }
                ),
            ),
            ("unknown row field", table(unknown=1.0)),
            (
                "missing row field",
                mutated(
                    nativeParameters={
                        **step["nativeParameters"],
                        "materialParametersByMaterial": {
                            "Si": {name: value for name, value in FLUOROCARBON_MATERIAL_DEFAULTS.items() if name != "K"}
                        },
                    }
                ),
            ),
            ("nan row field", table(density=float("nan"))),
            ("bool row field", table(beta_e=True)),
            ("out-of-range row field", table(density=1_000_001.0)),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = FluorocarbonNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=fluorocarbon_project(),
        )
        self.assertEqual(variant.validate(ctx, FluorocarbonParams(duration=1.0)), [])

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
        order = tuple(material_order(domain))
        state = ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=context.dimension,
                grid_delta=context.grid_delta,
            ),
            materials=MaterialState(order=order, names=_material_names(domain)),
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
                    fingerprint="fluorocarbon-plugin-test",
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
        project = fluorocarbon_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference, MATERIAL_TABLE)
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
        self.assertEqual(metadata["nativeClass"], "ps.FluorocarbonEtching")
        self.assertEqual(metadata["nativeConstructor"], "FluorocarbonParameters object overload")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], 0.2)
        self.assertEqual(metadata["materialParametersByMaterial"], MATERIAL_TABLE)
        self.assertEqual(metadata["materialDefaultsApplied"], [])
        self.assertEqual(metadata["materials"], list(MATERIAL_NAMES))
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
        self.assertEqual(identity["processId"], "fluorocarbon")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "fluorocarbon-plugin-test")

    def test_masked_trench_matches_the_native_reference(self) -> None:
        project = fluorocarbon_project(
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

        _reference(self.ps, project, reference, MATERIAL_TABLE)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        order = material_order(new_domain)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = fluorocarbon_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.1)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        _reference(self.ps, project, reference, MATERIAL_TABLE)
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

    def test_cpu_triangle_matches_direct_reference(self) -> None:
        project = fluorocarbon_project(2, x_extent=2.0, duration=0.1)
        reference = plane_domain(2, x_extent=2.0)
        candidate = plane_domain(2, x_extent=2.0)

        _reference(self.ps, project, reference, MATERIAL_TABLE, kernel="CPU_TRIANGLE")
        outcome = self._run(project, candidate, kernel="CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "CPU_TRIANGLE")
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self._assert_parity(reference, outcome.state.geometry.handle, 2)

    def test_missing_material_rows_are_filled_from_defaults(self) -> None:
        project = fluorocarbon_project(2, native_parameters={"materialParametersByMaterial": {}})
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, MATERIAL_TABLE)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["materialDefaultsApplied"], list(MATERIAL_NAMES))
        self.assertEqual(outcome.metadata["materialParametersByMaterial"], MATERIAL_TABLE)

    def test_explicit_material_rows_are_applied(self) -> None:
        custom = dict(FLUOROCARBON_MATERIAL_DEFAULTS)
        custom["A_ie"] = 0.05
        custom["density"] = 1.8
        table = _material_table(Si=custom)
        project = fluorocarbon_project(2, native_parameters={"materialParametersByMaterial": table})
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference, table)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertEqual(outcome.metadata["materialDefaultsApplied"], [])
        self.assertEqual(outcome.metadata["materialParametersByMaterial"], table)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = fluorocarbon_project(2)
        del project["solver"]
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        context = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=project, step=step, seed=RAY_SEED)
        variant = FluorocarbonNativeVariant()
        engine = SimpleNamespace(profile=SimpleNamespace(backend="cpu", kernel="CPU_DISK"), handle=ps)
        domain = plane_domain(2)
        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=tuple(material_order(domain)), names=_material_names(domain)),
        )
        with self.assertRaises(RuntimeError) as raised:
            variant.execute(state, resolved[1], context, engine)
        self.assertIn("solver", str(raised.exception))

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.fluorocarbon\n"
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
