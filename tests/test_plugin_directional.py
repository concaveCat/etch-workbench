"""Tests for the native v2 directional plugin.

Every execution test registers ``directional``/``native`` in a **local**
:class:`ProcessRegistry` and runs it through :func:`process_models.v2.resolver.run_step`
with a :class:`StaticEngine` wrapping the live ``viennaps`` module. The shared
plugin registry and ``plugins.lock`` are never consulted: adding an in-tree
plugin makes the lock stale until it is regenerated, which is out of scope for
this test module.

Each result is compared with a direct ``ps.DirectionalProcess`` reference
through :func:`worker.process_models_v2.compare_domains`. The directional model
has no stochastic flux engine, so no ray seeding proxy is needed and every run
is pinned to ``CPU_DISK``. No server or worker process is involved, and no test
here uses a GPU.
"""

from __future__ import annotations

import math
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps
from pydantic import ValidationError

from plugins.directional import (
    DirectionalNativeVariant,
    DirectionalParams,
    DirectionalProcessSpec,
    DirectionalRateSet,
    parse_step,
    register,
)
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    ExecutionProfile,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import (
    configure_advection,
    material_handle,
    protected_material_names,
)
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import run_step
from process_models.v2.state import LevelSetState, MaterialState, ProcessState, StepResult
from worker.process_models_v2 import compare_domains, material_order

ROOT = Path(__file__).resolve().parents[1]
GRID_DELTA = 0.2
X_EXTENT = 4.0
VIENNAPS_VERSION = str(ps.__version__)
ENGINE_FINGERPRINT = "directional-plugin-test"

PLANE_GEOMETRY: dict[str, Any] = {
    "kind": "plane",
    "height": 0.0,
    "substrateMaterial": "Si",
}
TRENCH_GEOMETRY: dict[str, Any] = {
    "kind": "trench",
    "width": 1.6,
    "depth": 1.2,
    "taperAngle": 0.0,
    "maskHeight": 0.6,
    "maskTaperAngle": 0.0,
    "substrateMaterial": "Si",
    "maskMaterial": "Mask",
}


def _solver() -> dict[str, Any]:
    return {
        "timeStepRatio": 0.25,
        "cpuThreads": 1,
        "random": {"mode": "fixed", "seed": 314159},
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


def scalar_step(
    *,
    operation: str = "etch",
    direction: Any = (0.0, -1.0, 0.0),
    directional: Any = 0.3,
    isotropic: Any = 0.1,
    visibility: Any = True,
    duration: Any = 0.4,
    deposit_material: Any = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "step_directional",
        "name": "Directional",
        "enabled": True,
        "operation": operation,
        "model": "directional",
        "direction": list(direction),
        "directionalRateMagnitude": directional,
        "isotropicRateMagnitude": isotropic,
        "calculateVisibility": visibility,
        "duration": duration,
    }
    if deposit_material is not None:
        step["depositMaterial"] = deposit_material
    return step


def rate_set_step(
    *,
    operation: str = "etch",
    rate_sets: Any = None,
    duration: Any = 0.3,
    deposit_material: Any = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "step_rate_sets",
        "name": "Directional rate sets",
        "enabled": True,
        "operation": operation,
        "model": "directional",
        "duration": duration,
        "rateSets": (
            [
                {
                    "direction": [0.0, -1.0, 0.0],
                    "directionalVelocity": 0.15,
                    "isotropicVelocity": 0.05,
                },
                {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.1},
            ]
            if rate_sets is None
            else rate_sets
        ),
    }
    if deposit_material is not None:
        step["depositMaterial"] = deposit_material
    return step


def material_rate_step(
    *,
    operation: str = "etch",
    direction: Any = (0.0, -1.0, 0.0),
    table: Any = None,
    default: Any = None,
    duration: Any = 0.3,
    deposit_material: Any = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "step_material_rates",
        "name": "Directional material rates",
        "enabled": True,
        "operation": operation,
        "model": "directional",
        "direction": list(direction),
        "materialRates": ({"Si": {"directional": 0.2, "isotropic": 0.05}} if table is None else table),
        "duration": duration,
    }
    if default is not None:
        step["defaultMaterialRate"] = default
    if deposit_material is not None:
        step["depositMaterial"] = deposit_material
    return step


def directional_project(
    step: dict[str, Any],
    *,
    dimension: int = 2,
    geometry: dict[str, Any] | None = None,
    grid_delta: float = GRID_DELTA,
    x_extent: float = X_EXTENT,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": grid_delta, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = x_extent
        domain["boundaryXY"] = "reflective"
    return {
        "schemaVersion": 7,
        "name": f"directional plugin test {dimension}D",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": dict(geometry or PLANE_GEOMETRY),
        "steps": [step],
        "solver": _solver(),
    }


def native_domain(project: dict[str, Any]) -> Any:
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    grid_delta = float(project["domain"]["gridDelta"])
    x_extent = float(project["domain"]["xExtent"])
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    if dimension == 2:
        domain = ps.Domain(gridDelta=grid_delta, xExtent=x_extent, boundary=boundary)
    else:
        domain = ps.Domain(
            gridDelta=grid_delta,
            xExtent=x_extent,
            yExtent=float(project["domain"]["yExtent"]),
            boundary=boundary,
        )
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(
            domain,
            height=float(geometry["height"]),
            material=material_handle(ps, geometry["substrateMaterial"]),
            addToExisting=False,
        ).apply()
    elif geometry["kind"] == "trench":
        ps.MakeTrench(
            domain=domain,
            trenchWidth=float(geometry["width"]),
            trenchDepth=float(geometry["depth"]),
            trenchTaperAngle=float(geometry["taperAngle"]),
            maskHeight=float(geometry["maskHeight"]),
            maskTaperAngle=float(geometry["maskTaperAngle"]),
            halfTrench=False,
            material=material_handle(ps, geometry["substrateMaterial"]),
            maskMaterial=material_handle(ps, geometry["maskMaterial"]),
        ).apply()
    else:
        raise ValueError(f"unsupported test geometry {geometry['kind']!r}")
    return domain


def _unit(direction: Any) -> list[float]:
    values = [float(component) for component in direction]
    length = math.sqrt(sum(component * component for component in values))
    return [component / length for component in values]


def native_reference(project: dict[str, Any], domain: Any) -> Any:
    """Run the direct native call the worker makes, with no plugin code."""
    step = project["steps"][0]
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(material_handle(ps, step["depositMaterial"]))

    sign = -1.0 if step["operation"] == "etch" else 1.0
    if step.get("rateSets") is not None:
        default_masks = protected_material_names(project, step)
        rate_sets = []
        for item in step["rateSets"]:
            requested = item.get("maskMaterials")
            masks = default_masks if requested is None else list(requested)
            directional = float(item["directionalVelocity"])
            rate_sets.append(
                ps.RateSet(
                    _unit(item["direction"]),
                    directional * sign,
                    float(item.get("isotropicVelocity", 0.0)) * sign,
                    [material_handle(ps, name) for name in masks],
                    bool(item.get("calculateVisibility", True)) and directional > 0.0,
                )
            )
        model = ps.DirectionalProcess(rate_sets)
    elif step.get("materialRates") is not None or step.get("defaultMaterialRate") is not None:
        etch = step["operation"] == "etch"
        directional_sign = 1.0 if etch else -1.0
        isotropic_sign = -1.0 if etch else 1.0
        table = {
            material_handle(ps, name): (
                float(row.get("directional", 0.0)) * directional_sign,
                float(row.get("isotropic", 0.0)) * isotropic_sign,
            )
            for name, row in (step.get("materialRates") or {}).items()
        }
        default = step.get("defaultMaterialRate") or {}
        model = ps.DirectionalProcess(
            _unit(step["direction"]),
            table,
            float(default.get("directional", 0.0)) * directional_sign,
            float(default.get("isotropic", 0.0)) * isotropic_sign,
        )
    else:
        directional = float(step["directionalRateMagnitude"])
        masks = protected_material_names(project, step)
        model = ps.DirectionalProcess(
            _unit(step["direction"]),
            directional * sign,
            float(step["isotropicRateMagnitude"]) * sign,
            [material_handle(ps, name) for name in masks],
            bool(step["calculateVisibility"]) and directional > 0.0,
        )

    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection, _actual = configure_advection(ps, project)
    process.setParameters(advection)
    process.apply()
    return domain


def _top(domain: Any, dimension: int) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    axis = 1 if dimension == 2 else 2
    return max(float(node[axis]) for node in mesh.getNodes())


class DirectionalPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def _provider(self) -> StaticEngineProvider:
        return StaticEngineProvider(
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
                    fingerprint=ENGINE_FINGERPRINT,
                )
            ]
        )

    def _run(self, project: dict[str, Any], domain: Any) -> Any:
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
        return run_step(
            self.registry,
            self._provider(),
            spec.id,
            self.registry.default_variant_id(spec.id),
            params,
            context,
            state,
            compute=ComputePolicy(backend="cpu", kernel="CPU_DISK"),
        )

    def _assert_parity(self, reference: Any, candidate: Any, dimension: int) -> dict[str, Any]:
        result = compare_domains(reference, candidate, dimension)
        self.assertTrue(result["matched"], result["reasons"])
        self.assertEqual(result["oldDigest"], result["newDigest"])
        self.assertEqual(result["oldMaterialMap"], result["newMaterialMap"])
        self.assertEqual(result["oldMaterialMap"], list(material_order(candidate)))
        self.assertGreater(result["oldPointCount"], 0)
        self.assertEqual(result["oldPointCount"], result["newPointCount"])
        return result

    # -- identity / parameters --------------------------------------------

    def test_manifest_and_identity_match_the_contract(self) -> None:
        with open(ROOT / "plugins" / "directional" / "plugin.toml", "rb") as handle:
            manifest = tomllib.load(handle)
        self.assertEqual(manifest["api"], 1)
        self.assertEqual(manifest["id"], "directional")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["entrypoint"], "plugins.directional:register")

        self.assertEqual(DirectionalProcessSpec.id, "directional")
        self.assertEqual(DirectionalProcessSpec.version, "1.0.0")
        self.assertEqual(DirectionalProcessSpec.params_model, DirectionalParams)
        self.assertEqual(DirectionalProcessSpec.dimensions, (2, 3))
        self.assertEqual(DirectionalProcessSpec.operations, ("etch", "deposit"))
        self.assertEqual(DirectionalProcessSpec.default_variant, "native")
        self.assertIs(DirectionalProcessSpec.parse_step, parse_step)

        variant = DirectionalNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertTrue(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertIsNotNone(variant.capabilities.state_profile)
        assert variant.capabilities.state_profile is not None
        self.assertTrue(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(
            variant.engine.profiles,
            (ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
        )
        self.assertIsNone(variant.implementation_hash)
        self.assertEqual(
            variant.implementation_files[0][0],
            "process_models/v2/engines/viennaps.py",
        )

        self.assertIsInstance(self.registration, VariantRegistration)
        self.assertEqual(self.registration.process_id, "directional")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("directional", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_strictness_modes_and_frozen_model(self) -> None:
        params = DirectionalParams(
            operation="etch",
            duration=1.0,
            direction=[0.0, -1.0, 0.0],
            directionalRateMagnitude=0.3,
            isotropicRateMagnitude=0.1,
        )
        self.assertEqual(params.mode(), "scalar")
        self.assertEqual(params.unit_direction(), [0.0, -1.0, 0.0])

        rate_sets = DirectionalParams(
            operation="etch",
            duration=1.0,
            rateSets=[DirectionalRateSet(direction=[0.0, -1.0, 0.0], directionalVelocity=0.2)],
        )
        self.assertEqual(rate_sets.mode(), "rateSets")

        material = DirectionalParams(
            operation="etch",
            duration=1.0,
            direction=[0.0, -1.0, 0.0],
            defaultMaterialRate={"directional": 0.1},
        )
        self.assertEqual(material.mode(), "materialRates")

        invalid = (
            {"duration": 0.0},
            {"duration": -1.0},
            {"duration": float("nan")},
            {"direction": [0.0, 0.0, 0.0]},
            {"direction": [0.0, -1.0]},
            {"directionalRateMagnitude": -0.1},
            {"directionalRateMagnitude": 2.1},
            {"directionalRateMagnitude": True},
            {"unexpected": 1},
            {"calculateVisibility": 1},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    DirectionalParams(
                        **{
                            "operation": "etch",
                            "duration": 1.0,
                            "direction": [0.0, -1.0, 0.0],
                            "directionalRateMagnitude": 0.3,
                            "isotropicRateMagnitude": 0.1,
                            **overrides,
                        }
                    )
        with self.assertRaises(ValidationError):
            params.duration = 2.0  # frozen

    def test_parse_step_accepts_all_three_modes(self) -> None:
        project = directional_project(scalar_step())
        scalar = parse_step(project, project["steps"][0])
        self.assertIsInstance(scalar, DirectionalParams)
        assert scalar is not None
        self.assertEqual(scalar.mode(), "scalar")
        self.assertEqual(scalar.operation, "etch")
        self.assertEqual(scalar.unit_direction(), [0.0, -1.0, 0.0])
        self.assertTrue(scalar.calculateVisibility)

        rate_sets = rate_set_step(
            rate_sets=[
                {
                    "direction": [0.0, -1.0, 0.0],
                    "directionalVelocity": 0.2,
                    "isotropicVelocity": 0.05,
                    "maskMaterials": [],
                    "calculateVisibility": False,
                },
                {"direction": [0.6, -0.8, 0.0], "directionalVelocity": 0.1},
            ]
        )
        project = directional_project(rate_sets)
        parsed = parse_step(project, project["steps"][0])
        self.assertIsInstance(parsed, DirectionalParams)
        assert parsed is not None
        self.assertEqual(parsed.mode(), "rateSets")
        self.assertIsNotNone(parsed.rateSets)
        assert parsed.rateSets is not None
        self.assertEqual(len(parsed.rateSets), 2)
        self.assertEqual(parsed.rateSets[0].maskMaterials, [])
        self.assertFalse(parsed.rateSets[0].calculateVisibility)
        self.assertIsNone(parsed.rateSets[1].maskMaterials)

        material = material_rate_step(default={"directional": 0.0, "isotropic": 0.02})
        project = directional_project(material)
        parsed = parse_step(project, project["steps"][0])
        self.assertIsInstance(parsed, DirectionalParams)
        assert parsed is not None
        self.assertEqual(parsed.mode(), "materialRates")
        self.assertEqual(parsed.materialRates["Si"].directional, 0.2)
        self.assertEqual(parsed.materialRates["Si"].isotropic, 0.05)
        self.assertEqual(parsed.defaultMaterialRate.isotropic, 0.02)

        # Explicit nulls behave like omitted keys, matching the backend schema.
        explicit_null = scalar_step()
        explicit_null["rateSets"] = None
        explicit_null["materialRates"] = None
        explicit_null["defaultMaterialRate"] = None
        project = directional_project(explicit_null)
        parsed = parse_step(project, project["steps"][0])
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.mode(), "scalar")

    def test_parse_step_rejects_other_processes_and_unknown_keys(self) -> None:
        base = scalar_step()
        candidates = {
            "isotropic": {**base, "model": "isotropic"},
            "specialized": {**base, "model": "specialized"},
            "missing model": {key: value for key, value in base.items() if key != "model"},
            "unknown top-level": {**base, "unknownKey": 1},
            "ray parameters": {**base, "rayTracingParameters": {"raysPerPoint": 8}},
        }
        project = directional_project(base)
        for label, candidate in candidates.items():
            with self.subTest(step=label):
                self.assertIsNone(parse_step(project, candidate))
                self.assertIsNone(self.registry.resolve_step(project, candidate))

    def test_parse_step_rejects_malformed_values(self) -> None:
        base = scalar_step()
        scalar_missing_rate = {key: value for key, value in base.items() if key != "directionalRateMagnitude"}
        scalar_missing_direction = {key: value for key, value in base.items() if key != "direction"}
        deposit_without_material = scalar_step(operation="deposit")
        invalid = (
            ("missing duration", {key: value for key, value in base.items() if key != "duration"}),
            ("zero duration", {**base, "duration": 0.0}),
            ("negative duration", {**base, "duration": -1.0}),
            ("nan duration", {**base, "duration": float("nan")}),
            ("string duration", {**base, "duration": "0.5"}),
            ("missing operation", {key: value for key, value in base.items() if key != "operation"}),
            ("bad operation", {**base, "operation": "clean"}),
            ("missing direction", scalar_missing_direction),
            ("two-component direction", {**base, "direction": [0.0, -1.0]}),
            ("zero direction", {**base, "direction": [0.0, 0.0, 0.0]}),
            ("string direction", {**base, "direction": [0.0, "-1.0", 0.0]}),
            ("missing rate", scalar_missing_rate),
            ("negative rate", {**base, "directionalRateMagnitude": -0.1}),
            ("too-large rate", {**base, "isotropicRateMagnitude": 2.1}),
            ("non-bool visibility", {**base, "calculateVisibility": "yes"}),
            ("empty rateSets", rate_set_step(rate_sets=[])),
            (
                "too many rateSets",
                rate_set_step(rate_sets=[{"direction": [0.0, -1.0, 0.0], "directionalVelocity": 0.1}] * 9),
            ),
            ("rateSets object", {**base, "rateSets": {}}),
            (
                "rateSets missing direction",
                {**base, "rateSets": [{"directionalVelocity": 0.1}]},
            ),
            (
                "rateSets zero direction",
                {
                    **base,
                    "rateSets": [{"direction": [0.0, 0.0, 0.0], "directionalVelocity": 0.1}],
                },
            ),
            (
                "rateSets negative velocity",
                {
                    **base,
                    "rateSets": [{"direction": [0.0, -1.0, 0.0], "directionalVelocity": -0.1}],
                },
            ),
            (
                "rateSets unknown key",
                {
                    **base,
                    "rateSets": [
                        {
                            "direction": [0.0, -1.0, 0.0],
                            "directionalVelocity": 0.1,
                            "unknown": 1,
                        }
                    ],
                },
            ),
            (
                "rateSets duplicate masks",
                {
                    **base,
                    "rateSets": [
                        {
                            "direction": [0.0, -1.0, 0.0],
                            "directionalVelocity": 0.1,
                            "maskMaterials": ["Mask", "Mask"],
                        }
                    ],
                },
            ),
            (
                "rateSets with scalar field",
                {**rate_set_step(), "directionalRateMagnitude": 0.1},
            ),
            (
                "materialRates with rateSets",
                {**material_rate_step(), "rateSets": rate_set_step()["rateSets"]},
            ),
            (
                "materialRates with scalar rate",
                {**material_rate_step(), "directionalRateMagnitude": 0.1},
            ),
            (
                "materialRates without direction",
                {key: value for key, value in material_rate_step().items() if key != "direction"},
            ),
            (
                "materialRates all zero",
                material_rate_step(table={"Si": {"directional": 0.0, "isotropic": 0.0}}),
            ),
            (
                "materialRates visibility false",
                {**material_rate_step(), "calculateVisibility": False},
            ),
            (
                "materialRates unknown row key",
                material_rate_step(table={"Si": {"directional": 0.1, "unknown": 1}}),
            ),
            ("materialRates scalar row", material_rate_step(table={"Si": 0.1})),
            ("deposit without material", deposit_without_material),
            ("etch with depositMaterial", {**base, "depositMaterial": "W"}),
            (
                "depositMaterial non-string",
                scalar_step(operation="deposit", deposit_material=5),
            ),
        )
        project = directional_project(base)
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step(project, candidate)

    # -- execution ---------------------------------------------------------

    def test_two_dimensional_scalar_etch_matches_the_native_reference(self) -> None:
        project = directional_project(scalar_step())
        reference = native_domain(project)
        candidate = native_domain(project)
        before = _top(candidate, 2)

        native_reference(project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertIs(new_domain, candidate)
        self.assertLess(_top(new_domain, 2), before)
        self.assertIsInstance(outcome.result, StepResult)
        self.assertIs(outcome.delta, outcome.result.delta)
        self.assertIs(outcome.delta.geometry.handle, new_domain)
        self.assertEqual(outcome.delta.geometry.dimension, 2)
        self.assertEqual(outcome.delta.geometry.grid_delta, GRID_DELTA)
        self.assertEqual(outcome.delta.materials.order, material_order(new_domain))
        self.assertEqual(outcome.delta.materials.order, (int(ps.Material.Si),))
        self.assertEqual(outcome.result.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.DirectionalProcess")
        self.assertEqual(metadata["mode"], "scalar")
        self.assertEqual(metadata["operation"], "etch")
        self.assertEqual(metadata["duration"], 0.4)
        self.assertEqual(metadata["requestedDirection"], [0.0, -1.0, 0.0])
        self.assertEqual(metadata["nativeUnitDirection"], [0.0, -1.0, 0.0])
        self.assertEqual(metadata["nativeDirectionalVelocity"], -0.3)
        self.assertEqual(metadata["nativeIsotropicVelocity"], -0.1)
        self.assertTrue(metadata["calculateVisibility"])
        self.assertTrue(metadata["visibility"])
        self.assertEqual(metadata["masks"], [])
        self.assertEqual(metadata["maskMaterials"], [])
        self.assertEqual(metadata["maskMaterialIds"], [])
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["executionProfile"]["kernel"], "CPU_DISK")
        self.assertEqual(metadata["timeStepRatio"], 0.25)
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["levelSetCount"], 1)
        self.assertEqual(metadata["materialMap"], list(material_order(new_domain)))

        identity = outcome.plan.identity()
        self.assertEqual(identity["process_id"], "directional")
        self.assertEqual(identity["variant_id"], "native")
        self.assertEqual(identity["engine_id"], "viennaps")
        self.assertEqual(identity["engine_version"], VIENNAPS_VERSION)
        self.assertEqual(identity["engine_kernel"], "CPU_DISK")
        self.assertEqual(identity["engine_fingerprint"], ENGINE_FINGERPRINT)

    def test_two_dimensional_scalar_deposit_matches_the_native_reference(self) -> None:
        project = directional_project(
            scalar_step(
                operation="deposit",
                direction=(0.0, -1.0, 0.0),
                directional=0.25,
                isotropic=0.05,
                duration=0.3,
                deposit_material="W",
            )
        )
        reference = native_domain(project)
        candidate = native_domain(project)
        before = _top(candidate, 2)

        native_reference(project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertGreater(_top(new_domain, 2), before)
        self.assertEqual(outcome.metadata["mode"], "scalar")
        self.assertEqual(outcome.metadata["operation"], "deposit")
        self.assertEqual(outcome.metadata["depositMaterial"], "W")
        self.assertEqual(outcome.metadata["nativeDirectionalVelocity"], 0.25)
        self.assertEqual(outcome.metadata["nativeIsotropicVelocity"], 0.05)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)
        order = material_order(new_domain)
        self.assertIn(int(ps.Material.Si), order)
        self.assertIn(int(ps.Material.W), order)
        self.assertEqual(len(order), 2)
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_three_dimensional_scalar_etch_smoke(self) -> None:
        project = directional_project(
            scalar_step(
                direction=(0.0, 0.0, -1.0),
                directional=0.2,
                isotropic=0.05,
                duration=0.2,
            ),
            dimension=3,
            x_extent=2.0,
        )
        reference = native_domain(project)
        candidate = native_domain(project)
        before = _top(candidate, 3)

        native_reference(project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 3)
        self.assertLess(_top(new_domain, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

    def test_rate_sets_match_the_native_reference(self) -> None:
        project = directional_project(rate_set_step())
        reference = native_domain(project)
        candidate = native_domain(project)
        before = _top(candidate, 2)

        native_reference(project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertLess(_top(new_domain, 2), before)
        metadata = outcome.metadata
        self.assertEqual(metadata["mode"], "rateSets")
        self.assertEqual(metadata["rateSetCount"], 2)
        self.assertEqual(metadata["nativeOverload"], "DirectionalProcess(rateSets: Sequence[RateSet])")
        self.assertEqual(
            [record["nativeDirectionalVelocity"] for record in metadata["rateSets"]],
            [-0.15, -0.1],
        )
        self.assertEqual(
            [record["nativeIsotropicVelocity"] for record in metadata["rateSets"]],
            [-0.05, -0.0],
        )
        self.assertEqual(
            [record["nativeUnitDirection"] for record in metadata["rateSets"]],
            [[0.0, -1.0, 0.0], [0.6, -0.8, 0.0]],
        )
        self.assertEqual(
            [record["maskSource"] for record in metadata["rateSets"]],
            ["geometry", "geometry"],
        )
        self.assertEqual(
            [record["requestedMaskMaterials"] for record in metadata["rateSets"]],
            [None, None],
        )
        self.assertEqual(metadata["masks"], [[], []])
        self.assertEqual(metadata["visibility"], [True, True])

    def test_rate_set_explicit_mask_list_is_honoured(self) -> None:
        project = directional_project(
            rate_set_step(
                rate_sets=[
                    {
                        "direction": [0.0, -1.0, 0.0],
                        "directionalVelocity": 0.2,
                        "isotropicVelocity": 0.02,
                    },
                    {
                        "direction": [0.0, -1.0, 0.0],
                        "directionalVelocity": 0.1,
                        "maskMaterials": [],
                    },
                ]
            ),
            geometry=TRENCH_GEOMETRY,
        )
        reference = native_domain(project)
        candidate = native_domain(project)

        native_reference(project, reference)
        outcome = self._run(project, candidate)

        self._assert_parity(reference, outcome.state.geometry.handle, 2)
        metadata = outcome.metadata
        self.assertEqual(
            [record["maskSource"] for record in metadata["rateSets"]],
            ["geometry", "request"],
        )
        self.assertEqual(
            [record["requestedMaskMaterials"] for record in metadata["rateSets"]],
            [None, []],
        )
        self.assertEqual(metadata["masks"], [["Mask"], []])
        self.assertEqual(
            [record["maskMaterialIds"] for record in metadata["rateSets"]],
            [[int(ps.Material.Mask)], []],
        )

    def test_material_rates_etch_signs_match_the_native_reference(self) -> None:
        project = directional_project(material_rate_step(default={"directional": 0.0, "isotropic": 0.02}))
        reference = native_domain(project)
        candidate = native_domain(project)
        before = _top(candidate, 2)

        native_reference(project, reference)
        outcome = self._run(project, candidate)
        new_domain = outcome.state.geometry.handle

        self._assert_parity(reference, new_domain, 2)
        self.assertLess(_top(new_domain, 2), before)
        metadata = outcome.metadata
        self.assertEqual(metadata["mode"], "materialRates")
        self.assertEqual(metadata["directionalSign"], 1.0)
        self.assertEqual(metadata["isotropicSign"], -1.0)
        self.assertEqual(metadata["materialTable"], {"Si": [0.2, -0.05]})
        self.assertEqual(
            metadata["nativeMaterialRatesById"],
            {str(int(ps.Material.Si)): [0.2, -0.05]},
        )
        self.assertEqual(
            metadata["defaultMaterialRate"],
            {"directional": 0.0, "isotropic": 0.02},
        )
        self.assertFalse(metadata["defaultMaterialRateOmitted"])
        self.assertEqual(metadata["nativeDefaultDirectionalRate"], 0.0)
        self.assertEqual(metadata["nativeDefaultIsotropicRate"], -0.02)
        self.assertIn("not-applicable", metadata["visibility"])
        self.assertEqual(metadata["materialRates"][0]["name"], "Si")
        self.assertEqual(metadata["materialRates"][0]["id"], int(ps.Material.Si))

    def test_masked_trench_matches_the_native_reference(self) -> None:
        step = scalar_step(directional=0.2, isotropic=0.0, duration=0.25)
        project = directional_project(step, geometry=TRENCH_GEOMETRY)
        self.assertEqual(protected_material_names(project, step), ["Mask"])
        reference = native_domain(project)
        candidate = native_domain(project)

        native_reference(project, reference)
        outcome = self._run(project, candidate)

        self._assert_parity(reference, outcome.state.geometry.handle, 2)
        metadata = outcome.metadata
        self.assertEqual(metadata["maskMaterials"], ["Mask"])
        self.assertEqual(metadata["maskMaterialIds"], [int(ps.Material.Mask)])
        order = material_order(outcome.state.geometry.handle)
        self.assertEqual(sorted(order), sorted((int(ps.Material.Si), int(ps.Material.Mask))))
        self.assertEqual(outcome.delta.materials.order, order)

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.directional\n"
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
