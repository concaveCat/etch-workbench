"""Tests for the native v2 selective SiGe epitaxy plugin.

Every execution test builds a small 2D/3D Si plane, registers the
``selectiveEpitaxy``/``native`` plugin in a **local**
:class:`~process_models.v2.registry.ProcessRegistry`, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` holding the test module.
The shared plugin registry, ``load_registry()``, ``run_v2_step()`` and
``plugins.lock`` are never consulted: adding an in-tree plugin makes the
checked-in lock stale until it is regenerated, which is out of scope here.

The result is compared with a direct ``ps.SelectiveEpitaxy`` + ``ps.Process``
reference through :func:`worker.process_models_v2.compare_domains`. That
reference duplicates the top level set as SiGe first, exactly like the plugin
and the stage-8 direct-reference deposit path, because the native model grows
the new SiGe level set rather than converting the substrate. Both paths use the
installed native material enums (``getattr(ps.Material, name)``): the epitaxy
constructor checks that identity, so a registry handle is not equivalent.

Only CPU_DISK is exercised. The model consumes no flux and no ray parameters,
so no seeding proxy is needed.
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
from plugins.selectiveEpitaxy import (
    SelectiveEpitaxyNativeVariant,
    SelectiveEpitaxyParams,
    SelectiveEpitaxyProcessSpec,
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
from process_models.v2.engines.viennaps import configure_advection
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-selectiveEpitaxy"
PLUGIN_TOML = ROOT / "plugins" / "selectiveEpitaxy" / "plugin.toml"

GRID_DELTA = 0.2
X_EXTENT = 2.0
VIENNAPS_VERSION = str(ps.__version__)
ENGINE_FINGERPRINT = "test-selectiveEpitaxy"

#: ``backend.specialized_models`` defaults for the ``selectiveEpitaxy`` scalars.
CATALOG_DEFAULTS = {"rate111": 0.5, "rate100": 1.0}

#: The upstream selectiveEpitaxy example rates.
DEFAULT_RATES = {"Si": 0.5, "SiGe": 0.5}

#: An explicit ``nvFactors`` vector that differs from the 2D native default.
EXPLICIT_NV_FACTORS = [0.8, 0.6, 0.0]


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
        "enginePreference": "cpu",
        "advection": {
            # The upstream selectiveEpitaxy example uses the stencil local
            # Lax-Friedrichs scheme; the default Engquist-Osher scheme does not
            # grow this flat surface.
            "spatialScheme": "STENCIL_LOCAL_LAX_FRIEDRICHS_1ST_ORDER",
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


def selective_step(
    *,
    duration: Any = 1.0,
    material_rates: dict[str, Any] | None = None,
    rate111: Any = None,
    rate100: Any = None,
    nv_factors: Any = None,
    deposit_material: Any = "SiGe",
) -> dict[str, Any]:
    parameters: dict[str, Any] = {"materialRates": dict(DEFAULT_RATES if material_rates is None else material_rates)}
    parameters.update(CATALOG_DEFAULTS)
    if rate111 is not None:
        parameters["rate111"] = rate111
    if rate100 is not None:
        parameters["rate100"] = rate100
    if nv_factors is not None:
        parameters["nvFactors"] = nv_factors
    step: dict[str, Any] = {
        "id": "step_selectiveEpitaxy",
        "name": "SiGe epitaxy",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "selectiveEpitaxy",
        "operation": "deposit",
        "duration": duration,
        "nativeParameters": parameters,
    }
    if deposit_material is not None:
        step["depositMaterial"] = deposit_material
    return step


def selective_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    duration: Any = 1.0,
    material_rates: dict[str, Any] | None = None,
    rate111: Any = None,
    rate100: Any = None,
    nv_factors: Any = None,
    deposit_material: Any = "SiGe",
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"selectiveEpitaxy plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            selective_step(
                duration=duration,
                material_rates=material_rates,
                rate111=rate111,
                rate100=rate100,
                nv_factors=nv_factors,
                deposit_material=deposit_material,
            )
        ],
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


def _reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Build the selective-epitaxy deposit path directly, with no plugin code.

    The top level set is duplicated as SiGe before ``ps.Process`` is built,
    because ``SelectiveEpitaxy`` grows that new level set. Rows use the native
    material enum, not a registry handle.
    """
    step = project["steps"][0]
    values = step["nativeParameters"]

    domain.duplicateTopLevelSet(ps_module.Material.SiGe)
    rates = [(getattr(ps_module.Material, name), float(rate)) for name, rate in sorted(values["materialRates"].items())]
    if "nvFactors" not in values:
        model = ps_module.SelectiveEpitaxy(rates, float(values["rate111"]), float(values["rate100"]))
    else:
        model = ps_module.SelectiveEpitaxy(
            [float(value) for value in values["nvFactors"]],
            float(values["rate111"]),
            float(values["rate100"]),
        )
        for material, rate in rates:
            model.setMaterialRate(material, rate)

    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    process.apply()
    return domain


def _unrated_nv_reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """The nvFactors overload without the mandatory ``setMaterialRate`` calls.

    The installed model then has an empty rate table and refuses its top
    material, which is exactly why the plugin must apply every rate after
    construction.
    """
    step = project["steps"][0]
    values = step["nativeParameters"]
    domain.duplicateTopLevelSet(ps_module.Material.SiGe)
    model = ps_module.SelectiveEpitaxy(
        [float(value) for value in values["nvFactors"]],
        float(values["rate111"]),
        float(values["rate100"]),
    )
    process = ps_module.Process(domain, model, float(step["duration"]))
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


class SelectiveEpitaxyPluginTest(unittest.TestCase):
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
        self.assertEqual(manifest["id"], "selectiveEpitaxy")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.selectiveEpitaxy:register")

        self.assertEqual(SelectiveEpitaxyProcessSpec.id, "selectiveEpitaxy")
        self.assertEqual(SelectiveEpitaxyProcessSpec.version, "1.0.0")
        self.assertEqual(SelectiveEpitaxyProcessSpec.params_model, SelectiveEpitaxyParams)
        self.assertEqual(SelectiveEpitaxyProcessSpec.dimensions, (2, 3))
        self.assertEqual(SelectiveEpitaxyProcessSpec.operations, ("deposit",))
        self.assertEqual(SelectiveEpitaxyProcessSpec.default_variant, "native")
        self.assertIs(SelectiveEpitaxyProcessSpec.parse_step, parse_step)

        variant = SelectiveEpitaxyNativeVariant()
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
        self.assertNotIn(("gpu", "GPU_DISK"), profiles)
        self.assertNotIn(("gpu", "GPU_TRIANGLE"), profiles)
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "selectiveEpitaxy")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("selectiveEpitaxy", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("selectiveEpitaxy"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("selectiveEpitaxy")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), {"materialRates", "rate111", "rate100"})
        self.assertEqual(list(fields), ["materialRates", "rate111", "rate100"])
        self.assertEqual(list(SelectiveEpitaxyParams.model_fields)[1:4], ["materialRates", "rate111", "rate100"])
        self.assertIn("nvFactors", {field["key"] for field in spec["optionalFields"]})

        params = SelectiveEpitaxyParams(duration=1.0, materialRates=DEFAULT_RATES)
        self.assertEqual(params.materialRates, DEFAULT_RATES)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
                model_field = SelectiveEpitaxyParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertIsNone(params.nvFactors)
        self.assertNotIn("coverageTolerance", SelectiveEpitaxyParams.model_fields)
        self.assertFalse(hasattr(params, "coverageTolerance"))
        self.assertEqual(params.sorted_material_rates(), (("Si", 0.5), ("SiGe", 0.5)))
        self.assertEqual(
            params.native_parameters(),
            {"materialRates": DEFAULT_RATES, "rate111": 0.5, "rate100": 1.0, "nvFactors": None},
        )

        with_factors = SelectiveEpitaxyParams(duration=1.0, materialRates=DEFAULT_RATES, nvFactors=EXPLICIT_NV_FACTORS)
        self.assertEqual(with_factors.nvFactors, EXPLICIT_NV_FACTORS)
        self.assertEqual(with_factors.native_parameters()["nvFactors"], EXPLICIT_NV_FACTORS)

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"duration": "1.0"},
            {"duration": True},
            {"materialRates": {}},
            {"materialRates": {"Si": -0.1}},
            {"materialRates": {"Si": float("inf")}},
            {"materialRates": {"Si": float("nan")}},
            {"materialRates": {"Si": True}},
            {"materialRates": {"Si": "0.5"}},
            {"materialRates": {"": 0.5}},
            {"materialRates": [("Si", 0.5)]},
            {"rate111": -0.1},
            {"rate111": float("inf")},
            {"rate100": -0.1},
            {"rate100": float("nan")},
            {"nvFactors": [0.5, 1.0]},
            {"nvFactors": [0.5, 1.0, 0.0, 0.2]},
            {"nvFactors": [0.5, 1.0, "0.0"]},
            {"nvFactors": [0.5, 1.0, float("nan")]},
            {"nvFactors": [0.5, True, 0.0]},
            {"nvFactors": "abc"},
            {"coverageTolerance": 1e-3},
            {"advanced": {"precursors": {"rate": 0.1}}},
            {"unexpected": 1},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    SelectiveEpitaxyParams(**{"duration": 1.0, "materialRates": DEFAULT_RATES, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_fields_and_defaults(self) -> None:
        project = selective_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, SelectiveEpitaxyParams)
        assert params is not None
        self.assertEqual(params.duration, 1.0)
        self.assertEqual(params.materialRates, DEFAULT_RATES)
        self.assertEqual(params.rate111, 0.5)
        self.assertEqual(params.rate100, 1.0)
        self.assertIsNone(params.nvFactors)

        explicit_project = selective_project(
            material_rates={"Si": 0.2, "SiGe": 0.4},
            rate111=0.3,
            rate100=0.7,
            nv_factors=EXPLICIT_NV_FACTORS,
        )
        explicit = parse_step(explicit_project, explicit_project["steps"][0])
        self.assertIsNotNone(explicit)
        assert explicit is not None
        self.assertEqual(explicit.materialRates, {"Si": 0.2, "SiGe": 0.4})
        self.assertEqual(explicit.rate111, 0.3)
        self.assertEqual(explicit.rate100, 0.7)
        self.assertEqual(explicit.nvFactors, EXPLICIT_NV_FACTORS)

        without_material = selective_project()
        del without_material["steps"][0]["depositMaterial"]
        self.assertIsInstance(parse_step(without_material, without_material["steps"][0]), SelectiveEpitaxyParams)

        claimed = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertIs(claimed[0], SelectiveEpitaxyProcessSpec)
        self.assertIsInstance(claimed[1], SelectiveEpitaxyParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = selective_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"precursors": {"rate": 0.1}}
        unknown = mutated()
        unknown["nativeParameters"]["coverageTolerance"] = 1e-3
        other_model = mutated(nativeModel="teosDeposition")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="etch")
        other_material = mutated(depositMaterial="Si")
        null_material = mutated(depositMaterial=None)

        for label, candidate in (
            ("advanced", advanced),
            ("coverageTolerance", unknown),
            ("teosDeposition", other_model),
            ("isotropic", not_specialized),
            ("etch", other_operation),
            ("Si", other_material),
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
            ("missing rates", {**step, "nativeParameters": {"rate111": 0.5, "rate100": 1.0}}),
            ("empty rates", {**step, "nativeParameters": {**step["nativeParameters"], "materialRates": {}}}),
            (
                "negative rate",
                {**step, "nativeParameters": {**step["nativeParameters"], "materialRates": {"Si": -0.1}}},
            ),
            (
                "nan rate",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "materialRates": {"Si": float("nan")},
                    },
                },
            ),
            (
                "bool rate",
                {
                    **step,
                    "nativeParameters": {**step["nativeParameters"], "materialRates": {"Si": True}},
                },
            ),
            ("negative rate111", {**step, "nativeParameters": {**step["nativeParameters"], "rate111": -0.1}}),
            ("nan rate100", {**step, "nativeParameters": {**step["nativeParameters"], "rate100": float("nan")}}),
            ("two nv factors", {**step, "nativeParameters": {**step["nativeParameters"], "nvFactors": [0.5, 1.0]}}),
            (
                "four nv factors",
                {**step, "nativeParameters": {**step["nativeParameters"], "nvFactors": [0.5, 1.0, 0.0, 0.2]}},
            ),
            (
                "string nv factor",
                {**step, "nativeParameters": {**step["nativeParameters"], "nvFactors": [0.5, "1.0", 0.0]}},
            ),
            (
                "nan nv factor",
                {**step, "nativeParameters": {**step["nativeParameters"], "nvFactors": [0.5, float("nan"), 0.0]}},
            ),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = SelectiveEpitaxyNativeVariant()
        ctx = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=selective_project())
        params = SelectiveEpitaxyParams(duration=1.0, materialRates=DEFAULT_RATES)
        self.assertEqual(variant.validate(ctx, params), [])

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
        """Register, resolve, and execute the plugin without the shared lock."""
        dimension = int(project["dimension"])
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        state = ProcessState(
            geometry=LevelSetState(
                handle=domain,
                dimension=dimension,
                grid_delta=project["domain"]["gridDelta"],
            ),
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
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="selectiveEpitaxy",
                variant_id="native",
                params=params,
                context=context,
                state=state,
                compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
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

    def _assert_sige_name(self, materials: MaterialState) -> None:
        sige_id = int(ps.Material.SiGe)
        self.assertEqual(materials.name_for(sige_id), "SiGe")
        self.assertEqual(materials.id_for("SiGe"), sige_id)
        self.assertEqual(materials.name_for(int(ps.Material.Si)), "Si")

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = selective_project(2, duration=1.0)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        after = _top(new_domain, 2)
        self.assertGreater(after, before)
        self.assertAlmostEqual(after - before, 0.5, delta=0.02)

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
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.SiGe)))
        self._assert_sige_name(delta.materials)
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.SelectiveEpitaxy")
        self.assertEqual(metadata["overload"], "materialRates")
        self.assertEqual(metadata["nativeConstructor"], "viennaps.d2.SelectiveEpitaxy(materialRates, rate111, rate100)")
        self.assertEqual(metadata["operation"], "deposit")
        self.assertEqual(metadata["depositMaterial"], "SiGe")
        self.assertTrue(metadata["duplicateTopLevelSet"])
        self.assertIsNone(metadata["nvFactors"])
        self.assertEqual(metadata["rates"], DEFAULT_RATES)
        self.assertEqual(metadata["rate111"], 0.5)
        self.assertEqual(metadata["rate100"], 1.0)
        self.assertEqual(metadata["duration"], 1.0)
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 2)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "selectiveEpitaxy")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = selective_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.5)
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
        self._assert_sige_name(outcome.delta.materials)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["overload"], "materialRates")
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_nv_factors_overload_matches_reference_and_applies_material_rates(self) -> None:
        project = selective_project(2, duration=1.0, nv_factors=EXPLICIT_NV_FACTORS)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        self.assertEqual(outcome.delta.materials.order, (int(ps.Material.Si), int(ps.Material.SiGe)))
        self._assert_sige_name(outcome.delta.materials)

        metadata = outcome.metadata
        self.assertEqual(metadata["overload"], "nvFactors")
        self.assertEqual(
            metadata["nativeConstructor"],
            "viennaps.d2.SelectiveEpitaxy(nvFactors, rate111, rate100) + setMaterialRate",
        )
        self.assertEqual(metadata["nvFactors"], EXPLICIT_NV_FACTORS)
        self.assertEqual(metadata["rates"], DEFAULT_RATES)
        self.assertEqual(metadata["parameters"]["nvFactors"], EXPLICIT_NV_FACTORS)

        # Omitting the post-construction setMaterialRate calls leaves the
        # native rate table empty and the model rejects its own top material.
        with self.assertRaises(RuntimeError) as raised:
            _unrated_nv_reference(self.ps, project, plane_domain(2))
        self.assertIn("not an epitaxy material", str(raised.exception))

    def test_material_map_names_the_new_sige_level_set(self) -> None:
        project = selective_project(2, duration=0.2)
        domain = plane_domain(2)
        self.assertEqual(material_order(domain), (int(ps.Material.Si),))

        outcome = self._run(project, domain)
        materials = outcome.delta.materials
        self.assertEqual(materials.order, (int(ps.Material.Si), int(ps.Material.SiGe)))
        self.assertEqual(materials.names, {int(ps.Material.Si): "Si", int(ps.Material.SiGe): "SiGe"})
        self._assert_sige_name(materials)
        self.assertEqual(outcome.metadata["materialMap"], [int(ps.Material.Si), int(ps.Material.SiGe)])
        self.assertEqual(outcome.metadata["levelSetCount"], 2)

    def test_explicit_parameter_values_are_applied_and_recorded(self) -> None:
        rates = {"Si": 0.2, "SiGe": 0.4}
        project = selective_project(2, duration=0.5, material_rates=rates, rate111=0.3, rate100=0.7)
        reference = plane_domain(2)
        candidate = plane_domain(2)

        _reference(self.ps, project, reference)
        outcome = self._run(project, candidate)

        self._assert_parity(reference, outcome.state.geometry.handle, 2)
        self.assertEqual(outcome.metadata["rates"], rates)
        self.assertEqual(outcome.metadata["rate111"], 0.3)
        self.assertEqual(outcome.metadata["rate100"], 0.7)
        self.assertEqual(outcome.metadata["duration"], 0.5)
        self._assert_sige_name(outcome.delta.materials)

    def test_incomplete_project_fails_loudly(self) -> None:
        project = selective_project(2)
        del project["solver"]
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        context = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
        )
        state = ProcessState(
            geometry=LevelSetState(handle=plane_domain(2), dimension=2, grid_delta=GRID_DELTA),
            materials=MaterialState(order=(int(ps.Material.Si),), names={int(ps.Material.Si): "Si"}),
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="selectiveEpitaxy",
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
            "import plugins.selectiveEpitaxy\n"
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
