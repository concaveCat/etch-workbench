"""Tests for the native v2 single-particle plasma plugin.

Each test registers the in-tree ``singleParticle``/``native`` plugin on a
manual :class:`~process_models.v2.registry.ProcessRegistry` and resolves it
against a ``StaticEngine`` whose handle is the installed ``viennaps`` module.
No plugin lock, worker bridge, server, or data directory is involved.

The reference path constructs ``ps.SingleParticleProcess`` and ``ps.Process``
directly with fixed ray tracing (``useRandomSeeds=False`` and a pinned
``rngSeed``), so a CPU run is bit-for-bit comparable with the plugin run.
Only the CPU profiles are exercised; GPU execution is intentionally not
covered here.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import numpy as np
import viennaps as ps
from pydantic import ValidationError

from plugins.singleParticle import (
    SingleParticleNativeVariant,
    SingleParticleParams,
    SingleParticleProcessSpec,
    parse_step,
    register,
)
from process_models.v2.contract import ProcessContext
from process_models.v2.engine import (
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState

ROOT = Path(__file__).resolve().parents[1]

GRID_DELTA = 0.2
X_EXTENT = 4.0
RAYS_PER_POINT = 32
RAY_SEED = 271828
VIENNAPS_VERSION = str(ps.__version__)
FINGERPRINT = "test-single-particle"


def _project(
    dimension: int = 2,
    *,
    geometry: dict[str, Any] | None = None,
    fixed_seed: bool = True,
) -> dict[str, Any]:
    domain: dict[str, Any] = {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT}
    if dimension == 2:
        domain["xBoundary"] = "reflective"
    else:
        domain["yExtent"] = X_EXTENT
        domain["boundaryXY"] = "reflective"
    solver: dict[str, Any] = {
        "timeStepRatio": 0.25,
        "raysPerPoint": RAYS_PER_POINT,
    }
    if fixed_seed:
        solver["random"] = {"mode": "fixed", "seed": RAY_SEED}
    return {
        "schemaVersion": 3,
        "dimension": dimension,
        "geometry": geometry or {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "solver": solver,
    }


def _plane(dimension: int = 2) -> Any:
    ps.setDimension(dimension)
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
    return domain


def _material_handle(name: str) -> Any:
    registry = ps.MaterialRegistry.instance()
    material = registry.findMaterial(name)
    if material is None:
        raise AssertionError(f"material {name!r} is absent from the native MaterialRegistry")
    return material


def _material_order(domain: Any) -> tuple[int, ...]:
    mapping = domain.getMaterialMap()
    return tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))


def _surface_points(domain: Any) -> np.ndarray:
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in node] for node in mesh.getNodes()]
    if not points:
        return np.zeros((0, 3), dtype=float)
    return np.asarray(points, dtype=float)


def _sorted_surface(domain: Any) -> np.ndarray:
    points = _surface_points(domain)
    if points.size == 0:
        return points
    return points[np.lexsort((points[:, 0], points[:, 1], points[:, 2]))]


def _top(domain: Any, dimension: int = 2) -> float:
    points = _surface_points(domain)
    if points.size == 0:
        raise AssertionError("domain has no surface nodes")
    vertical = 1 if dimension == 2 else 2
    return float(np.max(points[:, vertical]))


def _state(domain: Any, dimension: int = 2) -> ProcessState:
    return ProcessState(
        geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=GRID_DELTA),
        materials=MaterialState(order=_material_order(domain)),
    )


def _provider(version: str = VIENNAPS_VERSION) -> StaticEngineProvider:
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
                handle=ps,
                fingerprint=FINGERPRINT,
            )
        ]
    )


def _step_mapping(params: SingleParticleParams) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "step_single_particle",
        "name": "Single particle",
        "enabled": True,
        "model": "singleParticle",
        "operation": params.operation,
        "rateMagnitude": params.rateMagnitude,
        "duration": params.duration,
        "stickingProbability": params.stickingProbability,
        "sourceExponent": params.sourceExponent,
        "rateMode": params.rateMode,
    }
    if params.depositMaterial is not None:
        step["depositMaterial"] = params.depositMaterial
    if params.materialRates is not None:
        step["materialRates"] = dict(params.materialRates)
    return step


def _reference(
    project: dict[str, Any],
    domain: Any,
    params: SingleParticleParams,
    *,
    seed: int = RAY_SEED,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the native process directly, with no plugin code."""
    sign = -1.0 if params.operation == "etch" else 1.0
    signed_default = float(params.rateMagnitude) * sign

    if params.rateMode == "material":
        model = ps.SingleParticleProcess(
            rate=0.0,
            stickingProbability=float(params.stickingProbability),
            sourceExponent=float(params.sourceExponent),
            maskMaterials=[],
        )
        for name, rate in (params.materialRates or {}).items():
            model.setMaterialRate(_material_handle(name), float(rate) * sign)
    else:
        model = ps.SingleParticleProcess(
            rate=signed_default,
            stickingProbability=float(params.stickingProbability),
            sourceExponent=float(params.sourceExponent),
            maskMaterials=[],
        )

    if params.operation == "deposit":
        assert params.depositMaterial is not None
        domain.duplicateTopLevelSet(_material_handle(params.depositMaterial))

    process = ps.Process(domain, model, float(params.duration))
    process.setFluxEngineType(getattr(ps.FluxEngineType, kernel))

    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)

    rays = ps.RayTracingParameters()
    rays.raysPerPoint = RAYS_PER_POINT
    rays.rngSeed = int(seed)
    rays.useRandomSeeds = False
    process.setParameters(rays)

    process.apply()
    return domain


class SingleParticleNativePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def _params(self, **overrides: Any) -> SingleParticleParams:
        values: dict[str, Any] = {
            "operation": "etch",
            "rateMagnitude": 0.2,
            "duration": 0.2,
            "stickingProbability": 0.5,
            "sourceExponent": 1.0,
        }
        values.update(overrides)
        return SingleParticleParams(**values)

    def _run(
        self,
        params: SingleParticleParams,
        domain: Any,
        project: dict[str, Any],
        *,
        dimension: int = 2,
        seed: int | None = RAY_SEED,
    ) -> Any:
        ctx = ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project=project,
            step=_step_mapping(params),
            seed=seed,
        )
        request = ExecutionRequest(
            process_id="singleParticle",
            variant_id="native",
            params=params,
            context=ctx,
            state=_state(domain, dimension),
        )
        return resolve_execution(request, self.registry, _provider()).run()

    def _assert_parity(self, reference: Any, candidate: Any) -> None:
        np.testing.assert_array_equal(_sorted_surface(reference), _sorted_surface(candidate))
        self.assertEqual(_material_order(reference), _material_order(candidate))

    # -- identity / registration ------------------------------------------

    def test_registration_exposes_stable_identity_and_cpu_profiles(self) -> None:
        self.assertEqual(SingleParticleProcessSpec.id, "singleParticle")
        self.assertEqual(SingleParticleProcessSpec.version, "1.0.0")
        self.assertEqual(SingleParticleProcessSpec.params_model, SingleParticleParams)
        self.assertEqual(SingleParticleProcessSpec.dimensions, (2, 3))
        self.assertEqual(SingleParticleProcessSpec.operations, ("etch", "deposit"))
        self.assertEqual(SingleParticleProcessSpec.default_variant, "native")
        self.assertIs(SingleParticleProcessSpec.parse_step, parse_step)

        variant = SingleParticleNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertTrue(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertTrue(variant.capabilities.state_profile.allow_new_materials)
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")
        self.assertEqual(
            [profile.kernel for profile in variant.engine.profiles],
            ["CPU_DISK", "CPU_TRIANGLE", "GPU_DISK"],
        )
        self.assertNotIn("GPU_TRIANGLE", [profile.kernel for profile in variant.engine.profiles])
        self.assertIsNone(variant.implementation_hash)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertEqual(self.registration.process_id, "singleParticle")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("singleParticle", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_are_strict_frozen_and_mode_checked(self) -> None:
        params = self._params()
        self.assertEqual(params.rateMode, "uniform")
        self.assertIsNone(params.depositMaterial)
        self.assertIsNone(params.materialRates)
        self.assertEqual(params.signed_material_rates(), {})

        material = self._params(rateMode="material", materialRates={"Si": 0.3})
        self.assertEqual(material.signed_material_rates(), {"Si": -0.3})
        deposit = self._params(
            operation="deposit",
            depositMaterial="SiO2",
            rateMode="material",
            materialRates={"SiO2": 0.1},
        )
        self.assertEqual(deposit.signed_material_rates(), {"SiO2": 0.1})

        invalid = (
            {"operation": "isotropic"},
            {"rateMagnitude": -0.1},
            {"rateMagnitude": 2.1},
            {"rateMagnitude": float("inf")},
            {"duration": 0.0},
            {"duration": float("nan")},
            {"stickingProbability": -0.1},
            {"stickingProbability": 1.1},
            {"sourceExponent": -0.1},
            {"sourceExponent": 20.1},
            {"operation": "deposit"},
            {"depositMaterial": "SiO2"},
            {"depositMaterial": ""},
            {"materialRates": {"Si": 0.1}},
            {"rateMode": "material"},
            {"rateMode": "material", "materialRates": {"Si": 2.1}},
            {"rateMode": "material", "materialRates": {"Si": -0.1}},
            {"rateMode": "material", "materialRates": {"": 0.1}},
            {"rateMode": "material", "materialRates": "Si"},
            {"unexpected": 1},
            {"rateMagnitude": "0.2"},
            {"rateMagnitude": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self._params(**overrides)

        with self.assertRaises(ValidationError):
            params.duration = 1.0  # frozen

    # -- parse / rejection matrix -----------------------------------------

    def test_parse_step_accepts_uniform_material_and_deposit_steps(self) -> None:
        uniform = _step_mapping(self._params())
        parsed = parse_step({}, uniform)
        self.assertIsInstance(parsed, SingleParticleParams)
        assert parsed is not None
        self.assertEqual(parsed.operation, "etch")
        self.assertEqual(parsed.rateMagnitude, 0.2)
        self.assertEqual(parsed.duration, 0.2)
        self.assertEqual(parsed.stickingProbability, 0.5)
        self.assertEqual(parsed.sourceExponent, 1.0)
        self.assertIsNone(parsed.materialRates)

        material = _step_mapping(
            self._params(rateMode="material", materialRates={"Si": 0.3, "SiO2": 0.05}),
        )
        parsed = parse_step({}, material)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.rateMode, "material")
        self.assertEqual(parsed.materialRates, {"Si": 0.3, "SiO2": 0.05})

        deposit = _step_mapping(
            self._params(operation="deposit", depositMaterial="SiO2"),
        )
        parsed = parse_step({}, deposit)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.operation, "deposit")
        self.assertEqual(parsed.depositMaterial, "SiO2")

        # A UI-only selectivity reference is accepted and never passed through.
        referenced = {**uniform, "selectivityReference": "Si"}
        parsed = parse_step({}, referenced)
        self.assertIsNotNone(parsed)
        self.assertFalse(hasattr(parsed, "selectivityReference"))

    def test_parse_step_returns_none_for_other_models_and_advanced_fields(self) -> None:
        base = _step_mapping(self._params())
        rejected = (
            ("isotropic", {**base, "model": "isotropic"}),
            ("multiParticle", {**base, "model": "multiParticle"}),
            ("calculateVisibility", {**base, "calculateVisibility": True}),
            ("nativeParameters", {**base, "nativeParameters": {"advanced": {"x": 1}}}),
            ("advanced", {**base, "advanced": {"x": 1}}),
            ("isotropicMethod", {**base, "isotropicMethod": "geometric"}),
            ("isotropicTimeStepRatio", {**base, "isotropicTimeStepRatio": 0.2}),
            ("unknown", {**base, "polymerFlux": 0.2}),
        )
        for label, candidate in rejected:
            with self.subTest(step=label):
                self.assertIsNone(parse_step({}, candidate))

    def test_parse_step_raises_for_missing_or_malformed_values(self) -> None:
        base = _step_mapping(self._params())

        missing = (
            ("duration", {key: value for key, value in base.items() if key != "duration"}),
            ("rateMagnitude", {key: value for key, value in base.items() if key != "rateMagnitude"}),
            ("stickingProbability", {key: value for key, value in base.items() if key != "stickingProbability"}),
            ("sourceExponent", {key: value for key, value in base.items() if key != "sourceExponent"}),
        )
        for label, candidate in missing:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step({}, candidate)

        invalid = (
            ("string duration", {**base, "duration": "0.2"}),
            ("zero duration", {**base, "duration": 0.0}),
            ("nan rate", {**base, "rateMagnitude": float("nan")}),
            ("rate too large", {**base, "rateMagnitude": 2.1}),
            ("bool sticking", {**base, "stickingProbability": True}),
            ("sticking too large", {**base, "stickingProbability": 1.1}),
            ("source too large", {**base, "sourceExponent": 21.0}),
            ("invalid operation", {**base, "operation": "isotropic"}),
            ("deposit without material", {**base, "operation": "deposit"}),
            ("etch with deposit material", {**base, "depositMaterial": "SiO2"}),
            ("uniform with rates", {**base, "materialRates": {"Si": 0.1}}),
            ("material without rates", {**base, "rateMode": "material"}),
            ("material rate too large", {**base, "rateMode": "material", "materialRates": {"Si": 2.1}}),
            ("material string rate", {**base, "rateMode": "material", "materialRates": {"Si": "0.1"}}),
            ("material rates list", {**base, "rateMode": "material", "materialRates": [0.1]}),
            ("empty material name", {**base, "rateMode": "material", "materialRates": {"": 0.1}}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step({}, candidate)

    # -- execution ---------------------------------------------------------

    def test_two_dimensional_uniform_etch_matches_native_reference(self) -> None:
        project = _project(2)
        params = self._params()
        reference = _plane(2)
        candidate = _plane(2)
        before = _top(candidate, 2)

        _reference(project, reference, params)
        outcome = self._run(params, candidate, project)

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertLess(_top(outcome.state.geometry.handle, 2), before)
        self.assertEqual(outcome.delta.geometry.dimension, 2)
        self.assertEqual(outcome.delta.materials.order, _material_order(candidate))
        self.assertEqual(outcome.warnings, [])

        metadata = outcome.metadata
        self.assertEqual(metadata["nativeClass"], "ps.SingleParticleProcess")
        self.assertEqual(metadata["operation"], "etch")
        self.assertEqual(metadata["rateMode"], "uniform")
        self.assertEqual(metadata["requestedRate"], 0.2)
        self.assertAlmostEqual(metadata["signedRate"], -0.2)
        self.assertAlmostEqual(metadata["nativeDefaultRate"], -0.2)
        self.assertEqual(metadata["materialRates"], {})
        self.assertEqual(metadata["nativeMaterialRates"], {})
        self.assertEqual(metadata["stickingProbability"], 0.5)
        self.assertEqual(metadata["sourceExponent"], 1.0)
        self.assertIsNone(metadata["depositMaterial"])
        self.assertEqual(metadata["maskMaterials"], [])
        self.assertEqual(metadata["fluxEngineType"], "CPU_DISK")
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["levelSetCount"], 1)
        self.assertEqual(metadata["materialMap"], list(_material_order(candidate)))
        self.assertEqual(outcome.plan.identity_json()["processId"], "singleParticle")

    def test_two_dimensional_material_mode_matches_native_reference(self) -> None:
        project = _project(2)
        params = self._params(rateMode="material", materialRates={"Si": 0.3})
        reference = _plane(2)
        candidate = _plane(2)

        _reference(project, reference, params)
        outcome = self._run(params, candidate, project)

        self._assert_parity(reference, outcome.state.geometry.handle)
        metadata = outcome.metadata
        self.assertEqual(metadata["rateMode"], "material")
        self.assertAlmostEqual(metadata["signedRate"], -0.2)
        self.assertEqual(metadata["nativeDefaultRate"], 0.0)
        self.assertEqual(metadata["materialRates"], {"Si": 0.3})
        self.assertAlmostEqual(metadata["nativeMaterialRates"]["Si"], -0.3)
        self.assertEqual(metadata["maskMaterials"], [])
        self.assertEqual(metadata["materialMap"], list(_material_order(candidate)))

    def test_two_dimensional_deposit_matches_native_reference(self) -> None:
        project = _project(2)
        params = self._params(operation="deposit", depositMaterial="SiO2")
        reference = _plane(2)
        candidate = _plane(2)
        before = _top(candidate, 2)

        _reference(project, reference, params)
        outcome = self._run(params, candidate, project)

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertGreater(_top(outcome.state.geometry.handle, 2), before)
        order = _material_order(candidate)
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.SiO2)))
        self.assertEqual(outcome.delta.materials.order, order)
        self.assertEqual(outcome.metadata["depositMaterial"], "SiO2")
        self.assertAlmostEqual(outcome.metadata["signedRate"], 0.2)
        self.assertEqual(outcome.metadata["levelSetCount"], 2)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

    def test_project_fixed_seed_is_used_when_the_context_has_none(self) -> None:
        project = _project(2)
        params = self._params()
        reference = _plane(2)
        candidate = _plane(2)

        _reference(project, reference, params, seed=RAY_SEED)
        outcome = self._run(params, candidate, project, seed=None)

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertEqual(outcome.metadata["rayTracing"]["rngSeed"], RAY_SEED)
        self.assertFalse(outcome.metadata["rayTracing"]["useRandomSeeds"])

    def test_three_dimensional_plane_smoke_matches_native_reference(self) -> None:
        project = _project(3)
        params = self._params(rateMagnitude=0.2, duration=0.1)
        reference = _plane(3)
        candidate = _plane(3)
        before = _top(candidate, 3)

        _reference(project, reference, params)
        outcome = self._run(params, candidate, project, dimension=3)

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertLess(_top(outcome.state.geometry.handle, 3), before)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.state.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(outcome.metadata["levelSetCount"], 1)

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.singleParticle\n"
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
