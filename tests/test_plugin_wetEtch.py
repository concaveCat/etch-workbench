"""Tests for the native v2 crystalline wet-etch plugin.

Each execution test builds one small 2D/3D level-set domain, registers the
``wetEtch``/``native`` plugin in a **local** :class:`ProcessRegistry`, and runs
it through :class:`FlowExecutor` with a :class:`StaticEngine` wrapping the live
``viennaps`` module. The shared plugin registry and ``plugins.lock`` are never
consulted: adding an in-tree plugin makes the lock stale until it is
regenerated, which is out of scope for this test module.

The result is compared with a direct ``ps.WetEtching`` + ``ps.Process``
reference through :func:`worker.process_models_v2.compare_domains`. The native
model does not use a flux field, so no ray parameters are configured and the
comparison is exact.

No server or worker process is involved; the v2 state store writes into
``var/test-plugin-wetEtch/``.
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
from plugins.wetEtch import (
    WetEtchNativeVariant,
    WetEtchParams,
    WetEtchProcessSpec,
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
from process_models.v2.engines.viennaps import configure_advection, material_handle
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from process_models.v2.storage import StateStore
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-wetEtch"

GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)

#: ``backend.specialized_models`` defaults for the ``wetEtch`` catalog row.
CATALOG_DEFAULTS: dict[str, Any] = {
    "materialRates": {},
    "useCrystallineRates": False,
    "direction100": [1.0, 0.0, 0.0],
    "direction010": [0.0, 1.0, 0.0],
    "rate100": 0.1,
    "rate110": 0.1,
    "rate111": 0.1,
    "rate311": 0.1,
}


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
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
    }


def wet_step(
    *,
    duration: Any = 0.1,
    native_parameters: dict[str, Any] | None = None,
    material_rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {"materialRates": {"Si": 0.1} if material_rates is None else material_rates}
    parameters.update(native_parameters or {})
    return {
        "id": "step_wet_etch",
        "name": "Wet etch",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "wetEtch",
        "operation": "etch",
        "duration": duration,
        "nativeParameters": parameters,
    }


def wet_project(
    dimension: int = 2,
    *,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
    geometry: dict[str, Any] | None = None,
    native_parameters: dict[str, Any] | None = None,
    material_rates: dict[str, Any] | None = None,
    duration: Any = 0.1,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"wetEtch plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            wet_step(
                duration=duration,
                native_parameters=native_parameters,
                material_rates=material_rates,
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


def _reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Build the native wet-etch path directly, with no worker or plugin code."""
    step = project["steps"][0]
    values = step["nativeParameters"]
    rows = [(material_handle(ps_module, name), float(rate)) for name, rate in sorted(values["materialRates"].items())]
    if values.get("useCrystallineRates", False):
        model = ps_module.WetEtching(
            list(values.get("direction100", CATALOG_DEFAULTS["direction100"])),
            list(values.get("direction010", CATALOG_DEFAULTS["direction010"])),
            float(values.get("rate100", CATALOG_DEFAULTS["rate100"])),
            float(values.get("rate110", CATALOG_DEFAULTS["rate110"])),
            float(values.get("rate111", CATALOG_DEFAULTS["rate111"])),
            float(values.get("rate311", CATALOG_DEFAULTS["rate311"])),
            rows,
        )
    else:
        model = ps_module.WetEtching(rows)
    process = ps_module.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps_module, project)
    process.setParameters(advection)
    process.apply()
    return domain


class WetEtchPluginTest(unittest.TestCase):
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
        self.assertEqual(WetEtchProcessSpec.id, "wetEtch")
        self.assertEqual(WetEtchProcessSpec.version, "1.0.0")
        self.assertEqual(WetEtchProcessSpec.params_model, WetEtchParams)
        self.assertEqual(WetEtchProcessSpec.dimensions, (2, 3))
        self.assertEqual(WetEtchProcessSpec.operations, ("etch",))
        self.assertEqual(WetEtchProcessSpec.default_variant, "native")
        self.assertIs(WetEtchProcessSpec.parse_step, parse_step)

        manifest = (ROOT / "plugins" / "wetEtch" / "plugin.toml").read_text()
        self.assertIn('id = "wetEtch"', manifest)
        self.assertIn('version = "1.0.0"', manifest)
        self.assertIn('entrypoint = "plugins.wetEtch:register"', manifest)
        self.assertIn("api = 1", manifest)

        variant = WetEtchNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertFalse(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertFalse(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        self.assertEqual(
            tuple((profile.backend, profile.kernel) for profile in variant.engine.profiles),
            (("cpu", "CPU_DISK"),),
        )
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "wetEtch")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("wetEtch", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("wetEtch")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(
            list(WetEtchParams.model_fields),
            ["duration", "materialRates", "useCrystallineRates", "direction100", "direction010"]
            + ["rate100", "rate110", "rate111", "rate311"],
        )

        params = WetEtchParams(duration=1.0)
        self.assertEqual(params.materialRates, {})
        self.assertFalse(params.useCrystallineRates)
        self.assertEqual(params.direction100, [1.0, 0.0, 0.0])
        self.assertEqual(params.direction010, [0.0, 1.0, 0.0])
        for name in ("rate100", "rate110", "rate111", "rate311"):
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), CATALOG_DEFAULTS[name])
                model_field = WetEtchParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])
        self.assertEqual(
            params.sorted_material_rates(),
            (),
        )
        self.assertEqual(params.native_parameters(), CATALOG_DEFAULTS)

        sorted_params = WetEtchParams(
            duration=1.0,
            materialRates={"SiO2": 0.02, "Si": 0.1},
        )
        self.assertEqual(sorted_params.materialRates, {"Si": 0.1, "SiO2": 0.02})
        self.assertEqual(sorted_params.sorted_material_rates(), (("Si", 0.1), ("SiO2", 0.02)))

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"duration": "1.0"},
            {"duration": True},
            {"materialRates": {"Si": -0.1}},
            {"materialRates": {"Si": float("inf")}},
            {"materialRates": {"Si": float("nan")}},
            {"materialRates": {"Si": "0.1"}},
            {"materialRates": [("Si", 0.1)]},
            {"materialRates": {"": 0.1}},
            {"useCrystallineRates": 1},
            {"direction100": [1.0, 0.0]},
            {"direction100": [0.0, 0.0, 0.0]},
            {"direction010": [0.0, float("nan"), 0.0]},
            {"direction010": "010"},
            {"rate100": -0.1},
            {"rate110": float("nan")},
            {"rate111": 1_000_001.0},
            {"rate311": True},
            {"unexpected": 1},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    WetEtchParams(**{"duration": 1.0, **overrides})

        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = wet_project()
        params = parse_step(project, project["steps"][0])
        self.assertIsInstance(params, WetEtchParams)
        assert params is not None
        self.assertEqual(params.duration, 0.1)
        self.assertEqual(params.materialRates, {"Si": 0.1})
        self.assertFalse(params.useCrystallineRates)
        self.assertEqual(params.rate100, 0.1)

        crystalline = wet_project(
            native_parameters={
                "useCrystallineRates": True,
                "direction100": [0, 1, 0],
                "direction010": [1, 0, 0],
                "rate100": 0.12,
                "rate110": 0.08,
                "rate111": 0.06,
                "rate311": 0.04,
            }
        )
        params = parse_step(crystalline, crystalline["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertTrue(params.useCrystallineRates)
        self.assertEqual(params.direction100, [0.0, 1.0, 0.0])
        self.assertEqual(params.direction010, [1.0, 0.0, 0.0])
        self.assertEqual(
            (params.rate100, params.rate110, params.rate111, params.rate311),
            (0.12, 0.08, 0.06, 0.04),
        )

        resolved = self.registry.resolve_step(project, project["steps"][0])
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertIs(resolved[0], WetEtchProcessSpec)
        self.assertIsInstance(resolved[1], WetEtchParams)

    def test_parse_step_rejection_matrix(self) -> None:
        project = wet_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated()
        advanced["nativeParameters"]["advanced"] = {"ions": {"meanEnergy": 60.0}}
        unknown = mutated()
        unknown["nativeParameters"]["coverageTolerance"] = 0.1
        other_model = mutated(nativeModel="sf6o2")
        not_specialized = mutated(model="isotropic")
        no_rates = mutated()
        no_rates["nativeParameters"] = {"useCrystallineRates": False}
        empty_rates = mutated()
        empty_rates["nativeParameters"] = {"materialRates": {}}
        other_operation = mutated(operation="deposit")

        for label, candidate in (
            ("advanced", advanced),
            ("coverageTolerance", unknown),
            ("sf6o2", other_model),
            ("isotropic", not_specialized),
            ("no materialRates", no_rates),
            ("empty materialRates", empty_rates),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))
        # Dispatch is ``model``/``nativeModel`` only, as specified. The project
        # validator rejects a deposit-labelled wetEtch step before dispatch, so
        # this process still only ever runs as an etch in a validated project.
        self.assertIsInstance(parse_step(project, other_operation), WetEtchParams)

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("string duration", mutated(duration="1.0")),
            ("bool duration", mutated(duration=True)),
            ("nan duration", mutated(duration=float("nan"))),
            ("zero duration", mutated(duration=0.0)),
            ("negative duration", mutated(duration=-1.0)),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
            ("rates type", {**step, "nativeParameters": {"materialRates": [1, 2, 3]}}),
            (
                "string rate",
                {**step, "nativeParameters": {"materialRates": {"Si": "0.1"}}},
            ),
            (
                "bool rate",
                {**step, "nativeParameters": {"materialRates": {"Si": True}}},
            ),
            (
                "nan rate",
                {**step, "nativeParameters": {"materialRates": {"Si": float("nan")}}},
            ),
            (
                "negative rate",
                {**step, "nativeParameters": {"materialRates": {"Si": -0.1}}},
            ),
            (
                "non-string material",
                {**step, "nativeParameters": {"materialRates": {1: 0.1}}},
            ),
            (
                "empty material name",
                {**step, "nativeParameters": {"materialRates": {"": 0.1}}},
            ),
            (
                "non-bool crystalline",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "useCrystallineRates": "false",
                    },
                },
            ),
            (
                "short direction",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "direction100": [1.0, 0.0],
                    },
                },
            ),
            (
                "zero direction",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "direction010": [0.0, 0.0, 0.0],
                    },
                },
            ),
            (
                "boolean direction component",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "direction100": [True, 0.0, 0.0],
                    },
                },
            ),
            (
                "negative crystalline rate",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "rate100": -0.1,
                    },
                },
            ),
            (
                "oversized crystalline rate",
                {
                    **step,
                    "nativeParameters": {
                        **step["nativeParameters"],
                        "rate311": 1_000_001.0,
                    },
                },
            ),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_requires_a_material_rate_row(self) -> None:
        variant = WetEtchNativeVariant()
        ctx = ProcessContext(dimension=2, grid_delta=GRID_DELTA, project=wet_project())
        issues = variant.validate(ctx, WetEtchParams(duration=1.0, materialRates={"Si": 0.1}))
        self.assertEqual(issues, [])
        issues = variant.validate(ctx, WetEtchParams(duration=1.0))
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].path, "materialRates")
        self.assertEqual(issues[0].severity, "error")

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
        """Run the registered plugin through a local registry and static engine.

        ``registry`` is the test's own :class:`ProcessRegistry`, so the shared
        plugin lock is never read. No ray parameters are set: the wet-etch
        model does not consume a flux field.
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
                    handle=ps,
                    fingerprint="wetEtch-plugin-test",
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

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = wet_project(2)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIsNot(new_domain, candidate)
        # The executor checkpoints the mutating variant: the input is untouched.
        self.assertTrue(compare_domains(plane_domain(2), candidate, 2)["matched"])
        after = _top(new_domain, 2)
        # ViennaLS stores material inside as negative; removing material moves
        # the exposed surface to a larger coordinate. The exact amount is the
        # native model's business, so the smoke check only proves motion.
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
        self.assertEqual(delta.materials.order, (int(ps.Material.Si),))
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.WetEtching")
        self.assertEqual(metadata["overload"], "materialRates")
        self.assertEqual(metadata["rateMode"], "material")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS | {"materialRates": {"Si": 0.1}})
        self.assertEqual(metadata["duration"], 0.1)
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["materialMap"], list(order))
        self.assertEqual(metadata["levelSetCount"], 1)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "wetEtch")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], "wetEtch-plugin-test")

    def test_masked_trench_matches_the_native_reference(self) -> None:
        rates = {"Mask": 0.0, "Si": 0.1}
        project = wet_project(
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
            material_rates=rates,
            duration=0.1,
        )
        reference = trench_domain()
        candidate = trench_domain()

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        order = material_order(new_domain)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self.assertEqual(outcome.metadata["parameters"]["materialRates"], rates)

    def test_three_dimensional_plane_smoke(self) -> None:
        grid_delta = 0.4
        project = wet_project(3, grid_delta=grid_delta, x_extent=2.0, duration=0.05)
        reference = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        candidate = plane_domain(3, grid_delta=grid_delta, x_extent=2.0)
        before = _top(candidate, 3)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertGreater(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], grid_delta)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_DISK")

    def test_crystalline_overload_matches_the_native_reference(self) -> None:
        native_parameters = {
            "useCrystallineRates": True,
            "direction100": [0.0, 1.0, 0.0],
            "direction010": [1.0, 0.0, 0.0],
            "rate100": 0.12,
            "rate110": 0.08,
            "rate111": 0.06,
            "rate311": 0.04,
        }
        project = wet_project(2, native_parameters=native_parameters, duration=0.1)
        reference = plane_domain(2)
        candidate = plane_domain(2)
        before = _top(candidate, 2)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        metadata = outcome.metadata
        self.assertEqual(metadata["overload"], "crystalline")
        self.assertEqual(metadata["rateMode"], "crystalline")
        self.assertEqual(metadata["parameters"]["direction100"], [0.0, 1.0, 0.0])
        self.assertEqual(metadata["parameters"]["direction010"], [1.0, 0.0, 0.0])
        self.assertEqual(
            (
                metadata["parameters"]["rate100"],
                metadata["parameters"]["rate110"],
                metadata["parameters"]["rate111"],
                metadata["parameters"]["rate311"],
            ),
            (0.12, 0.08, 0.06, 0.04),
        )
        self.assertEqual(metadata["parameters"]["materialRates"], {"Si": 0.1})
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")

    def test_incomplete_project_fails_loudly(self) -> None:
        project = wet_project(2)
        del project["solver"]
        step = project["steps"][0]
        resolved = self.registry.resolve_step(project, step)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        context = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
        )
        variant = WetEtchNativeVariant()
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
            "import plugins.wetEtch\n"
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
