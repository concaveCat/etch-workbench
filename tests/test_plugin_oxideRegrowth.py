"""Tests for the native v2 oxide-regrowth plugin.

The tests never call :func:`worker.process_models_v2.run_v2_step` or
``load_registry``: adding a plugin makes the checked-in ``plugins.lock`` stale,
and this suite must not depend on a lock that the main workspace owns. Instead
every test builds a manual :class:`~process_models.v2.registry.ProcessRegistry`,
registers ``plugins.oxideRegrowth`` into it, and resolves an
:class:`~process_models.v2.resolver.ExecutionRequest` against a
:class:`~process_models.v2.engine.StaticEngineProvider` whose handle is the test
module ``viennaps``. The lock is therefore never consulted.

Each execution test builds one small 2D/3D flat Si/Si3N4/SiO2 stack (the native
``OxideRegrowth`` model needs both SiO2 and Si3N4), runs the
``oxideRegrowth``/``native`` registration, and compares the result with a
direct ``ps.OxideRegrowth`` + ``ps.Process`` reference through
:func:`worker.process_models_v2.compare_domains`. The reference repeats the
worker's ``DenseCellSet`` derivation verbatim: surface top plus padding, GAS
fill, ``byproductSum`` field, and a built neighborhood. ``OxideRegrowth`` has no
flux model and no ray tracing, so the two runs are bit-for-bit comparable. Only
CPU profiles are tested: the plugin advertises ``CPU_DISK`` alone.

No server or worker process is involved; the v2 state store writes into
``var/test-plugin-oxideRegrowth/``.
"""

from __future__ import annotations

import json
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
from plugins.oxideRegrowth import (
    OxideRegrowthNativeVariant,
    OxideRegrowthParams,
    OxideRegrowthProcessSpec,
    parse_step,
    register,
)
from plugins.oxideRegrowth.params import FLAT_FIELDS, NUMERIC_FIELDS
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
TEST_ROOT = ROOT / "var" / "test-plugin-oxideRegrowth"
PLUGIN_TOML = ROOT / "plugins" / "oxideRegrowth" / "plugin.toml"

GRID_DELTA = 0.2
X_EXTENT = 4.0
DURATION = 1.0
NITRIDE_THICKNESS = 0.4
OXIDE_THICKNESS = 0.4
STACK_TOP = NITRIDE_THICKNESS + OXIDE_THICKNESS
VIENNAPS_VERSION = str(ps.__version__)
ENGINE_FINGERPRINT = "test-oxideRegrowth"

#: ``backend.specialized_models`` defaults for the ``oxideRegrowth`` catalog row.
CATALOG_DEFAULTS: dict[str, float] = {
    "nitrideEtchRate": 0.01,
    "oxideEtchRate": 0.005,
    "redepositionRate": 0.001,
    "redepositionThreshold": 0.01,
    "redepositionTimeInt": 0.1,
    "diffusionCoefficient": 0.02,
    "sinkStrength": 0.1,
    "scallopVelocity": 0.05,
    "centerVelocity": 0.05,
    "topHeight": 0.6,
    "centerWidth": 1.2,
    "stabilityFactor": 0.15,
}


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
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


def oxide_regrowth_step(
    *,
    duration: Any = DURATION,
    native_parameters: dict[str, Any] | None = None,
    **step_fields: Any,
) -> dict[str, Any]:
    parameters = dict(CATALOG_DEFAULTS)
    parameters.update(native_parameters or {})
    step: dict[str, Any] = {
        "id": "step_oxide_regrowth",
        "name": "Oxide regrowth",
        "enabled": True,
        "model": "specialized",
        "nativeModel": "oxideRegrowth",
        "operation": "native",
        "nativeParameters": parameters,
        "duration": duration,
    }
    step.update(step_fields)
    return step


def oxide_regrowth_project(
    dimension: int = 2,
    *,
    duration: Any = DURATION,
    native_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = X_EXTENT
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"oxideRegrowth plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {
            "kind": "stack",
            "baseHeight": 0.0,
            "substrateMaterial": "Si",
            "layers": [
                {"material": "Si3N4", "thickness": NITRIDE_THICKNESS, "role": "film"},
                {"material": "SiO2", "thickness": OXIDE_THICKNESS, "role": "film"},
            ],
            "pattern": {"kind": "none"},
        },
        "steps": [
            oxide_regrowth_step(duration=duration, native_parameters=native_parameters),
        ],
        "solver": _solver(),
    }


def flat_stack(dimension: int = 2) -> Any:
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    if dimension == 2:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=X_EXTENT,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    else:
        domain = ps.Domain(
            gridDelta=GRID_DELTA,
            xExtent=X_EXTENT,
            yExtent=X_EXTENT,
            boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
        )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    ps.MakePlane(
        domain,
        height=NITRIDE_THICKNESS,
        material=ps.Material.Si3N4,
        addToExisting=True,
    ).apply()
    ps.MakePlane(
        domain,
        height=STACK_TOP,
        material=ps.Material.SiO2,
        addToExisting=True,
    ).apply()
    return domain


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


def _derive_cell_set(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Repeat the worker's DenseCellSet derivation before the native model."""
    dimension = int(project["dimension"])
    axis = 1 if dimension == 2 else 2
    surface = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    top = max(float(point[axis]) for point in surface.getNodes())
    height = top + max(1.0, 5.0 * float(project["domain"]["gridDelta"]))
    domain.generateCellSet(height, ps_module.Material.GAS, True)
    cell_set = domain.getCellSet()
    if cell_set is None:
        raise AssertionError("reference Domain.generateCellSet returned no DenseCellSet")
    cell_set.addScalarData("byproductSum", 0.0)
    cell_set.buildNeighborhood()
    return cell_set


def _reference(ps_module: Any, project: dict[str, Any], domain: Any) -> Any:
    """Build the oxide-regrowth path directly, with no worker code."""
    values = project["steps"][0]["nativeParameters"]
    _derive_cell_set(ps_module, project, domain)
    model = ps_module.OxideRegrowth(*(float(values[name]) for name in FLAT_FIELDS))
    process = ps_module.Process(domain, model, float(project["steps"][0]["duration"]))
    process.setFluxEngineType(ps_module.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps_module, project)
    advection.ignoreVoids = True
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


MATERIAL_NAMES = {
    int(ps.Material.Si): "Si",
    int(ps.Material.Si3N4): "Si3N4",
    int(ps.Material.SiO2): "SiO2",
}


class OxideRegrowthPluginTest(unittest.TestCase):
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
        manifest = tomllib.loads(PLUGIN_TOML.read_text(encoding="utf-8"))
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "oxideRegrowth")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.oxideRegrowth:register")

        self.assertEqual(OxideRegrowthProcessSpec.id, "oxideRegrowth")
        self.assertEqual(OxideRegrowthProcessSpec.version, "1.0.0")
        self.assertEqual(OxideRegrowthProcessSpec.params_model, OxideRegrowthParams)
        self.assertEqual(OxideRegrowthProcessSpec.dimensions, (2, 3))
        self.assertEqual(OxideRegrowthProcessSpec.operations, ("native",))
        self.assertEqual(OxideRegrowthProcessSpec.default_variant, "native")
        self.assertIs(OxideRegrowthProcessSpec.parse_step, parse_step)

        variant = OxideRegrowthNativeVariant()
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
        self.assertIsNone(variant.implementation_hash)
        self.assertTrue(variant.implementation_files)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "oxideRegrowth")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("oxideRegrowth", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        self.assertEqual(self.registry.default_variant_id("oxideRegrowth"), "native")

    def test_params_defaults_and_ranges_match_the_catalog(self) -> None:
        spec = model_spec("oxideRegrowth")
        self.assertIsNotNone(spec)
        assert spec is not None
        fields = {field["key"]: field for field in spec["fields"]}
        self.assertEqual(set(fields), set(CATALOG_DEFAULTS))
        self.assertEqual(set(FLAT_FIELDS), set(CATALOG_DEFAULTS))
        self.assertEqual(set(NUMERIC_FIELDS), set(CATALOG_DEFAULTS))

        params = OxideRegrowthParams(duration=DURATION)
        for name, default in CATALOG_DEFAULTS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(params, name), default)
        for name in FLAT_FIELDS:
            with self.subTest(field=name):
                model_field = OxideRegrowthParams.model_fields[name]
                ge = next(meta.ge for meta in model_field.metadata if hasattr(meta, "ge"))
                le = next(meta.le for meta in model_field.metadata if hasattr(meta, "le"))
                self.assertEqual(ge, fields[name]["minimum"])
                self.assertEqual(le, fields[name]["maximum"])

        self.assertEqual(params.native_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.flat_arguments(), tuple(CATALOG_DEFAULTS[name] for name in FLAT_FIELDS))
        self.assertEqual(params.duration, DURATION)

        with self.assertRaises(ValidationError):
            OxideRegrowthParams()
        for duration in (0.0, -1.0, float("nan"), float("inf"), "1", True):
            with self.subTest(duration=duration):
                with self.assertRaises(ValidationError):
                    OxideRegrowthParams(duration=duration)
        for name in FLAT_FIELDS:
            for value in (-0.1, 1_000_001.0, float("nan"), "0.1", True):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValidationError):
                        OxideRegrowthParams(duration=DURATION, **{name: value})
        with self.assertRaises(ValidationError):
            OxideRegrowthParams(duration=DURATION, unexpected=1)
        with self.assertRaises(ValidationError):
            params.nitrideEtchRate = 0.02  # frozen

    def test_parse_step_maps_flat_fields_and_defaults(self) -> None:
        project = oxide_regrowth_project()
        step = project["steps"][0]

        params = parse_step(project, step)
        self.assertIsInstance(params, OxideRegrowthParams)
        assert params is not None
        self.assertEqual(params.native_values(), CATALOG_DEFAULTS)
        self.assertEqual(params.duration, DURATION)

        sparse = oxide_regrowth_project()
        sparse["steps"][0]["nativeParameters"] = {
            "nitrideEtchRate": 0.02,
            "stabilityFactor": 0.25,
        }
        sparse["steps"][0]["duration"] = 2.5
        params = parse_step(sparse, sparse["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.nitrideEtchRate, 0.02)
        self.assertEqual(params.stabilityFactor, 0.25)
        self.assertEqual(params.oxideEtchRate, CATALOG_DEFAULTS["oxideEtchRate"])
        self.assertEqual(params.duration, 2.5)

        without_parameters = oxide_regrowth_project()
        del without_parameters["steps"][0]["nativeParameters"]
        params = parse_step(without_parameters, without_parameters["steps"][0])
        self.assertIsNotNone(params)
        assert params is not None
        self.assertEqual(params.native_values(), CATALOG_DEFAULTS)

        claimed = self.registry.resolve_step(project, step)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed[0].id, "oxideRegrowth")
        self.assertIsInstance(claimed[1], OxideRegrowthParams)

    def test_parse_step_rejects_advanced_unknown_and_other_processes(self) -> None:
        project = oxide_regrowth_project()
        step = project["steps"][0]

        def mutated(**changes: Any) -> dict[str, Any]:
            copy = {**step, "nativeParameters": dict(step["nativeParameters"])}
            copy.update(changes)
            return copy

        advanced = mutated(advanced={"cflFactor": 0.5})
        advanced_model = mutated(advancedOxideRegrowth={"enabled": True})
        nested_advanced = mutated()
        nested_advanced["nativeParameters"]["advanced"] = {"cflFactor": 0.5}
        unknown = mutated()
        unknown["nativeParameters"]["unknownKey"] = 1
        other_model = mutated(nativeModel="oxidation")
        not_specialized = mutated(model="isotropic")
        other_operation = mutated(operation="deposit")

        for label, candidate in (
            ("advanced", advanced),
            ("advancedOxideRegrowth", advanced_model),
            ("nested advanced", nested_advanced),
            ("unknown", unknown),
            ("oxidation", other_model),
            ("isotropic", not_specialized),
            ("deposit", other_operation),
        ):
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

        invalid = (
            ("missing duration", {k: v for k, v in step.items() if k != "duration"}),
            ("parameters type", {**step, "nativeParameters": [1, 2, 3]}),
            ("duration string", {**step, "duration": "1"}),
            ("duration bool", {**step, "duration": True}),
            ("duration nan", {**step, "duration": float("nan")}),
            ("duration inf", {**step, "duration": float("inf")}),
            ("duration zero", {**step, "duration": 0.0}),
            ("duration negative", {**step, "duration": -1.0}),
            ("string rate", {**step, "nativeParameters": {**step["nativeParameters"], "nitrideEtchRate": "0.01"}}),
            ("bool rate", {**step, "nativeParameters": {**step["nativeParameters"], "nitrideEtchRate": True}}),
            ("nan rate", {**step, "nativeParameters": {**step["nativeParameters"], "nitrideEtchRate": float("nan")}}),
            ("negative rate", {**step, "nativeParameters": {**step["nativeParameters"], "oxideEtchRate": -0.1}}),
            ("too large", {**step, "nativeParameters": {**step["nativeParameters"], "topHeight": 1_000_001.0}}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    def test_validate_returns_no_issues(self) -> None:
        variant = OxideRegrowthNativeVariant()
        ctx = ProcessContext(
            dimension=2,
            grid_delta=GRID_DELTA,
            project=oxide_regrowth_project(),
        )
        self.assertEqual(variant.validate(ctx, OxideRegrowthParams(duration=DURATION)), [])

    # -- execution ---------------------------------------------------------

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
        """Register, resolve, and execute the plugin without the shared lock."""
        dimension = int(project["dimension"])
        step = project["steps"][0]
        params = parse_step(project, step)
        self.assertIsNotNone(params)
        state = ProcessState(
            geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=GRID_DELTA),
            materials=MaterialState(order=material_order(domain), names=dict(MATERIAL_NAMES)),
        )
        context = ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project=project,
            step=step,
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="oxideRegrowth",
                variant_id="native",
                params=params,
                context=context,
                state=state,
                compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
            ),
            self.registry,
            _cpu_provider(ps),
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

    def _assert_cell_set_outputs(self, outcome: Any, dimension: int) -> dict[str, Any]:
        metadata = outcome.metadata
        derived = metadata["derivedCellSet"]
        self.assertEqual(derived["nativeType"], "DenseCellSet")
        self.assertEqual(derived["material"], "GAS")
        self.assertEqual(derived["verticalAxis"], 1 if dimension == 2 else 2)
        self.assertEqual(derived["initialField"], "byproductSum=0")
        self.assertTrue(derived["neighborhoodBuilt"])
        self.assertAlmostEqual(
            derived["padding"],
            max(1.0, 5.0 * GRID_DELTA),
            places=9,
        )
        self.assertAlmostEqual(
            derived["height"],
            derived["top"] + derived["padding"],
            places=9,
        )

        cell_set = metadata["cellSet"]
        native = outcome.state.geometry.handle.getCellSet()
        self.assertIsNotNone(native)
        assert native is not None
        labels = [str(label) for label in native.getScalarDataLabels()]
        self.assertEqual(cell_set["nativeType"], "DenseCellSet")
        self.assertEqual(cell_set["numberOfCells"], int(native.getNumberOfCells()))
        self.assertEqual(cell_set["fieldLabels"], labels)
        self.assertIn("byproductSum", labels)
        self.assertEqual(set(cell_set["fieldStatistics"]), set(labels))
        for label in labels:
            statistics = cell_set["fieldStatistics"][label]
            values = [float(value) for value in native.getScalarData(label)]
            self.assertEqual(statistics["count"], len(values))
            self.assertEqual(statistics["min"], min(values))
            self.assertEqual(statistics["max"], max(values))

        self.assertEqual(len(outcome.artifacts), 1)
        artifact = outcome.artifacts[0]
        self.assertEqual(artifact.kind, "json")
        self.assertEqual(artifact.name, "nativeCellSet")
        self.assertEqual(artifact.schema_version, 1)
        self.assertIsNone(artifact.path)
        payload = artifact.metadata
        self.assertEqual(payload["nativeType"], "DenseCellSet")
        self.assertEqual(payload["dimension"], dimension)
        self.assertEqual(payload["gridDelta"], GRID_DELTA)
        self.assertEqual(payload["numberOfCells"], cell_set["numberOfCells"])
        self.assertEqual(payload["fieldLabels"], labels)
        self.assertEqual(set(payload["fields"]), set(labels))
        self.assertEqual(len(payload["fields"]["byproductSum"]), cell_set["numberOfCells"])
        self.assertEqual(payload["derivedSetup"], derived)
        return payload

    def test_two_dimensional_plane_matches_the_native_reference(self) -> None:
        project = oxide_regrowth_project(2)
        reference = flat_stack(2)
        candidate = flat_stack(2)
        before = _top(candidate, 2)

        _reference(ps, project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertLess(_top(new_domain, 2), before)
        self.assertEqual(_top(reference, 2), _top(new_domain, 2))

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
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertEqual(outcome.result.outputs.metrology, ())
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.OxideRegrowth")
        self.assertEqual(metadata["operation"], "native")
        self.assertEqual(metadata["parameters"], CATALOG_DEFAULTS)
        self.assertEqual(metadata["duration"], DURATION)
        self.assertEqual(metadata["durationDerivation"], "step.duration")
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertTrue(metadata["ignoreVoids"])
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertIsNone(metadata["rayTracing"])
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["levelSetCount"], 3)
        self.assertEqual(metadata["materialMap"], list(order))

        self._assert_cell_set_outputs(outcome, 2)

        identity = outcome.plan.identity_json()
        self.assertEqual(identity["processId"], "oxideRegrowth")
        self.assertEqual(identity["variantId"], "native")
        self.assertEqual(identity["engineId"], "viennaps")
        self.assertEqual(identity["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(identity["engineKernel"], "CPU_DISK")
        self.assertEqual(identity["engineFingerprint"], ENGINE_FINGERPRINT)

    def test_three_dimensional_plane_smoke(self) -> None:
        project = oxide_regrowth_project(3)
        candidate = flat_stack(3)
        before = _top(candidate, 3)

        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self.assertIs(new_domain, candidate)
        self.assertLess(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.delta.geometry.grid_delta, GRID_DELTA)
        order = material_order(new_domain)
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(len(order), 3)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(outcome.metadata["levelSetCount"], 3)
        self.assertEqual(outcome.metadata["materialMap"], list(order))
        self._assert_cell_set_outputs(outcome, 3)

    def test_cell_set_metadata_and_artifact_are_present(self) -> None:
        project = oxide_regrowth_project(2)
        outcome = self._run(project, flat_stack(2))

        payload = self._assert_cell_set_outputs(outcome, 2)
        self.assertEqual(
            payload["source"],
            "viennaps.Domain.getCellSet after OxideRegrowth.Process.apply",
        )
        self.assertIn("association", payload)
        self.assertGreater(payload["numberOfCells"], 0)
        self.assertEqual(
            set(payload["materialCounts"]),
            {
                str(int(ps.Material.GAS)),
                str(int(ps.Material.Si)),
                str(int(ps.Material.Si3N4)),
                str(int(ps.Material.SiO2)),
            },
        )
        for values in payload["fields"].values():
            self.assertTrue(all(isinstance(value, float) for value in values))

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_backend_or_native_module(self) -> None:
        code = (
            "import json\n"
            "import sys\n"
            "import plugins.oxideRegrowth\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name == 'worker' or name.startswith('worker.')\n"
            "    or name == 'backend' or name.startswith('backend.')\n"
            ")\n"
            "print(json.dumps({'leaked': leaked, 'native': 'viennaps' in sys.modules}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {"leaked": [], "native": False},
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
