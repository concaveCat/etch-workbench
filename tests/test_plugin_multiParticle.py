"""Tests for the native v2 multi-particle plasma plugin.

Each test registers the in-tree ``multiParticle``/``native`` plugin on a manual
:class:`~process_models.v2.registry.ProcessRegistry` and resolves it against a
``StaticEngine`` whose handle is the installed ``viennaps`` module. No plugin
lock, worker bridge, server, or data directory is involved.

The reference path constructs ``ps.MultiParticleProcess`` and ``ps.Process``
directly with the same fixed weighted-sum rate table, fixed ray tracing
(``useRandomSeeds=False`` and a pinned ``rngSeed``), and the same advection
parameters, so a CPU run is bit-for-bit comparable with the plugin run. Only
the CPU profiles are exercised; GPU execution and the deferred multi-ion split
strategy are intentionally not covered here.
"""

from __future__ import annotations

import math
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

import numpy as np
import viennaps as ps
from pydantic import ValidationError

from plugins.multiParticle import (
    MultiParticleNativeVariant,
    MultiParticleParams,
    MultiParticleProcessSpec,
    parse_step,
    register,
)
from process_models.v2.contract import ProcessContext
from process_models.v2.engine import (
    ComputePolicy,
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionRequest, resolve_execution
from process_models.v2.state import LevelSetState, MaterialState, ProcessState

ROOT = Path(__file__).resolve().parents[1]

GRID_DELTA = 0.2
X_EXTENT = 2.0
RAYS_PER_POINT = 16
RAY_SEED = 271828
VIENNAPS_VERSION = str(ps.__version__)
FINGERPRINT = "test-multi-particle"


def _project(dimension: int = 2, *, fixed_seed: bool = True) -> dict[str, Any]:
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
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
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
    return registry.getMaterial(name)


def _native_material_names() -> dict[str, str]:
    registry = ps.MaterialRegistry.instance()
    names: dict[str, str] = {}
    for name in dir(ps.Material):
        if name.startswith("_") or registry.findMaterial(name) is None:
            continue
        names[str(registry.getMaterial(name))] = name
    return names


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


def _species() -> list[dict[str, Any]]:
    return [
        {
            "kind": "ion",
            "label": "ion",
            "sourcePower": 1.0,
            "thetaRMin": 0.0,
            "thetaRMax": 90.0,
            "minAngle": 80.0,
            "B_sp": -1.0,
            "meanEnergy": 100.0,
            "sigmaEnergy": 10.0,
            "thresholdEnergy": 20.0,
            "inflectAngle": 0.0,
            "n": 1,
        },
        {
            "kind": "neutral",
            "label": "neutral",
            "defaultStickingProbability": 0.5,
            "stickingProbabilityByMaterial": {},
        },
    ]


def _defaults() -> dict[str, float]:
    return {"ion": 0.2, "neutral": 0.1}


def _params(**overrides: Any) -> MultiParticleParams:
    values: dict[str, Any] = {
        "operation": "etch",
        "duration": 0.1,
        "species": _species(),
        "rateCoefficients": {"defaultBySpecies": _defaults(), "byMaterial": {}},
    }
    values.update(overrides)
    return MultiParticleParams(**values)


def _step_mapping(params: MultiParticleParams) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "step_multi_particle",
        "name": "Multi particle",
        "enabled": True,
        "model": "multiParticle",
        "operation": params.operation,
        "duration": params.duration,
        "species": [species.model_dump() for species in params.species],
        "rateCoefficients": {
            "defaultBySpecies": params.default_coefficients(),
            "byMaterial": params.material_coefficients(),
        },
    }
    if params.depositMaterial is not None:
        step["depositMaterial"] = params.depositMaterial
    return step


def _reference(
    project: dict[str, Any],
    domain: Any,
    params: MultiParticleParams,
    *,
    seed: int = RAY_SEED,
    kernel: str = "CPU_DISK",
) -> Any:
    """Build the native process directly, with no plugin code."""
    labels = params.labels()
    defaults = params.default_coefficients()
    overrides = params.material_coefficients()
    sign = -1.0 if params.operation == "etch" else 1.0
    material_names = _native_material_names()

    if params.operation == "deposit":
        assert params.depositMaterial is not None
        domain.duplicateTopLevelSet(_material_handle(params.depositMaterial))

    model = ps.MultiParticleProcess()
    for species in params.species:
        if species.kind == "ion":
            model.addIonParticle(
                sourcePower=float(species.sourcePower),
                thetaRMin=float(species.thetaRMin),
                thetaRMax=float(species.thetaRMax),
                minAngle=float(species.minAngle),
                B_sp=float(species.B_sp),
                meanEnergy=float(species.meanEnergy),
                sigmaEnergy=float(species.sigmaEnergy),
                thresholdEnergy=float(species.thresholdEnergy),
                inflectAngle=float(species.inflectAngle),
                n=int(species.n),
                label=str(species.label),
            )
        else:
            model.addNeutralParticle(
                {
                    _material_handle(name): float(probability)
                    for name, probability in species.stickingProbabilityByMaterial.items()
                },
                defaultStickingProbability=float(species.defaultStickingProbability),
                label=str(species.label),
            )

    def rate_function(fluxes: Any, material: Any) -> float:
        values = [float(value) for value in fluxes]
        if len(values) != len(labels):
            raise AssertionError("reference callback received a mismatched flux vector")
        if any(not math.isfinite(value) for value in values):
            raise AssertionError("reference callback received a non-finite flux")
        material_name = material_names[str(material)]
        coefficients = overrides.get(material_name, {})
        return sign * sum(
            values[index] * coefficients.get(label, defaults[label]) for index, label in enumerate(labels)
        )

    model.setRateFunction(rate_function)
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


class MultiParticleNativePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def _run(
        self,
        params: MultiParticleParams,
        domain: Any,
        project: dict[str, Any],
        *,
        dimension: int = 2,
        seed: int | None = RAY_SEED,
        compute: ComputePolicy | None = None,
    ) -> Any:
        ctx = ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project=project,
            step=_step_mapping(params),
            seed=seed,
        )
        request = ExecutionRequest(
            process_id="multiParticle",
            variant_id="native",
            params=params,
            context=ctx,
            state=_state(domain, dimension),
            compute=compute,
        )
        return resolve_execution(request, self.registry, _provider()).run()

    def _assert_parity(self, reference: Any, candidate: Any) -> None:
        np.testing.assert_array_equal(_sorted_surface(reference), _sorted_surface(candidate))
        self.assertEqual(_material_order(reference), _material_order(candidate))

    # -- identity / registration ------------------------------------------

    def test_registration_exposes_stable_identity_and_cpu_only_profiles(self) -> None:
        self.assertEqual(MultiParticleProcessSpec.id, "multiParticle")
        self.assertEqual(MultiParticleProcessSpec.version, "1.0.0")
        self.assertEqual(MultiParticleProcessSpec.params_model, MultiParticleParams)
        self.assertEqual(MultiParticleProcessSpec.dimensions, (2, 3))
        self.assertEqual(MultiParticleProcessSpec.operations, ("etch", "deposit"))
        self.assertEqual(MultiParticleProcessSpec.default_variant, "native")
        self.assertIs(MultiParticleProcessSpec.parse_step, parse_step)

        variant = MultiParticleNativeVariant()
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
            [profile.kernel for profile in variant.engine.profiles],
            ["CPU_DISK", "CPU_TRIANGLE"],
        )
        self.assertTrue(all(profile.backend == "cpu" for profile in variant.engine.profiles))
        self.assertIsNone(variant.implementation_hash)
        self.assertEqual(variant.implementation_files[0][0], "process_models/v2/engines/viennaps.py")

        self.assertEqual(self.registration.process_id, "multiParticle")
        self.assertEqual(self.registration.variant_id, "native")
        resolved = self.registry.get_variant("multiParticle", "native")
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)

    def test_params_are_strict_frozen_and_species_checked(self) -> None:
        params = _params()
        self.assertEqual(params.labels(), ("ion", "neutral"))
        self.assertEqual(params.default_coefficients(), _defaults())
        self.assertEqual(params.material_coefficients(), {})
        self.assertEqual(params.species[0].kind, "ion")
        self.assertEqual(params.species[0].n, 1)
        self.assertEqual(params.species[1].defaultStickingProbability, 0.5)

        override = _params(
            rateCoefficients={
                "defaultBySpecies": _defaults(),
                "byMaterial": {"Si": {"ion": 0.4}},
            },
        )
        self.assertEqual(override.material_coefficients(), {"Si": {"ion": 0.4}})

        deposit = _params(operation="deposit", depositMaterial="SiO2")
        self.assertEqual(deposit.operation, "deposit")
        self.assertEqual(deposit.depositMaterial, "SiO2")

        invalid = (
            {"operation": "isotropic"},
            {"duration": 0.0},
            {"duration": float("nan")},
            {"duration": float("inf")},
            {"operation": "deposit"},
            {"depositMaterial": "SiO2"},
            {"depositMaterial": ""},
            {"species": []},
            {"species": _species() * 5},
            {"species": [{**_species()[0], "thetaRMin": 91.0}]},
            {"species": [{**_species()[0], "thetaRMin": 80.0, "thetaRMax": 70.0}]},
            {"species": [{**_species()[0], "sourcePower": 21.0}]},
            {"species": [{**_species()[0], "sourcePower": float("nan")}]},
            {"species": [{**_species()[0], "n": 0}]},
            {"species": [{**_species()[0], "n": 1.5}]},
            {"species": [{**_species()[1], "defaultStickingProbability": 1.1}]},
            {"species": [_species()[0], _species()[0]]},
            {"rateCoefficients": {"defaultBySpecies": {"ion": 0.2}}},
            {"rateCoefficients": {"defaultBySpecies": {**_defaults(), "extra": 0.1}}},
            {"rateCoefficients": {"defaultBySpecies": {"ion": -0.1, "neutral": 0.1}}},
            {"rateCoefficients": {"defaultBySpecies": {"ion": 2.1, "neutral": 0.1}}},
            {"rateCoefficients": {"defaultBySpecies": _defaults(), "byMaterial": {"Si": {"unknown": 0.1}}}},
            {"rateCoefficients": {"defaultBySpecies": _defaults(), "byMaterial": {"": {"ion": 0.1}}}},
            {"species": [{**_species()[0], "advanced": {}}]},
            {"unexpected": 1},
            {"duration": "0.1"},
            {"duration": True},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    _params(**overrides)

        with self.assertRaises(ValidationError):
            params.duration = 1.0  # frozen

    # -- parse / rejection matrix -----------------------------------------

    def test_parse_step_accepts_full_and_defaulted_steps(self) -> None:
        step = _step_mapping(_params())
        parsed = parse_step({}, step)
        self.assertIsInstance(parsed, MultiParticleParams)
        assert parsed is not None
        self.assertEqual(parsed.operation, "etch")
        self.assertEqual(parsed.duration, 0.1)
        self.assertEqual(parsed.labels(), ("ion", "neutral"))
        self.assertEqual(parsed.default_coefficients(), _defaults())

        defaulted = {
            "id": "step_defaulted",
            "name": "Defaulted",
            "enabled": True,
            "model": "multiParticle",
            "operation": "etch",
            "duration": 0.2,
            "species": [
                {"kind": "ion", "label": "ion2", "sourcePower": 0.5},
                {"kind": "neutral", "label": "neutral2"},
            ],
            "rateCoefficients": {"defaultBySpecies": {"ion2": 0.1, "neutral2": 0.05}},
        }
        parsed = parse_step({}, defaulted)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        ion = parsed.species[0]
        neutral = parsed.species[1]
        self.assertEqual(ion.thetaRMax, 90.0)
        self.assertEqual(ion.meanEnergy, 0.0)
        self.assertEqual(ion.n, 1)
        self.assertEqual(neutral.defaultStickingProbability, 1.0)
        self.assertEqual(neutral.stickingProbabilityByMaterial, {})

        material = _step_mapping(
            _params(
                rateCoefficients={
                    "defaultBySpecies": _defaults(),
                    "byMaterial": {"Si": {"ion": 0.3, "neutral": 0.0}},
                },
            ),
        )
        parsed = parse_step({}, material)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.material_coefficients(), {"Si": {"ion": 0.3, "neutral": 0.0}})

        deposit = _step_mapping(_params(operation="deposit", depositMaterial="SiO2"))
        parsed = parse_step({}, deposit)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.operation, "deposit")
        self.assertEqual(parsed.depositMaterial, "SiO2")

    def test_parse_step_returns_none_for_other_models_and_unknown_keys(self) -> None:
        base = _step_mapping(_params())
        rejected = (
            ("isotropic", {**base, "model": "isotropic"}),
            ("singleParticle", {**base, "model": "singleParticle"}),
            ("advanced", {**base, "advanced": {"x": 1}}),
            ("nativeParameters", {**base, "nativeParameters": {"advanced": {}}}),
            ("unknown", {**base, "polymerFlux": 0.2}),
            (
                "species unknown",
                {
                    **base,
                    "species": [{**_species()[0], "unknownOption": True}, _species()[1]],
                },
            ),
            (
                "coefficient unknown",
                {
                    **base,
                    "rateCoefficients": {
                        "defaultBySpecies": _defaults(),
                        "callback": "return 0",
                    },
                },
            ),
        )
        for label, candidate in rejected:
            with self.subTest(step=label):
                self.assertIsNone(parse_step({}, candidate))

    def test_parse_step_raises_for_missing_or_malformed_values(self) -> None:
        base = _step_mapping(_params())

        missing = (
            ("operation", {key: value for key, value in base.items() if key != "operation"}),
            ("duration", {key: value for key, value in base.items() if key != "duration"}),
            ("species", {key: value for key, value in base.items() if key != "species"}),
            ("rateCoefficients", {key: value for key, value in base.items() if key != "rateCoefficients"}),
        )
        for label, candidate in missing:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step({}, candidate)

        invalid = (
            ("invalid operation", {**base, "operation": "isotropic"}),
            ("string duration", {**base, "duration": "0.1"}),
            ("zero duration", {**base, "duration": 0.0}),
            ("nan duration", {**base, "duration": float("nan")}),
            ("species not array", {**base, "species": _species()[0]}),
            ("empty species", {**base, "species": []}),
            ("too many species", {**base, "species": _species() * 5}),
            ("species item type", {**base, "species": ["ion"]}),
            ("unknown kind", {**base, "species": [{**_species()[0], "kind": "electron"}]}),
            ("duplicate labels", {**base, "species": [_species()[0], _species()[0]]}),
            ("bad label", {**base, "species": [{**_species()[0], "label": "1ion"}]}),
            ("bool source", {**base, "species": [{**_species()[0], "sourcePower": True}]}),
            ("string source", {**base, "species": [{**_species()[0], "sourcePower": "1.0"}]}),
            ("nan source", {**base, "species": [{**_species()[0], "sourcePower": float("nan")}]}),
            ("source too large", {**base, "species": [{**_species()[0], "sourcePower": 21.0}]}),
            ("angles reversed", {**base, "species": [{**_species()[0], "thetaRMin": 80.0, "thetaRMax": 70.0}]}),
            ("n float", {**base, "species": [{**_species()[0], "n": 1.5}]}),
            ("n too small", {**base, "species": [{**_species()[0], "n": 0}]}),
            ("n bool", {**base, "species": [{**_species()[0], "n": True}]}),
            ("sticking too large", {**base, "species": [{**_species()[1], "defaultStickingProbability": 1.1}]}),
            (
                "sticking map type",
                {**base, "species": [{**_species()[1], "stickingProbabilityByMaterial": []}]},
            ),
            (
                "sticking map value",
                {
                    **base,
                    "species": [{**_species()[1], "stickingProbabilityByMaterial": {"Si": "0.5"}}],
                },
            ),
            ("coefficients type", {**base, "rateCoefficients": []}),
            (
                "defaults missing",
                {**base, "rateCoefficients": {"defaultBySpecies": {"ion": 0.2}}},
            ),
            (
                "defaults extra",
                {**base, "rateCoefficients": {"defaultBySpecies": {**_defaults(), "extra": 0.1}}},
            ),
            (
                "default negative",
                {**base, "rateCoefficients": {"defaultBySpecies": {"ion": -0.1, "neutral": 0.1}}},
            ),
            (
                "default string",
                {**base, "rateCoefficients": {"defaultBySpecies": {"ion": "0.2", "neutral": 0.1}}},
            ),
            (
                "material unknown label",
                {
                    **base,
                    "rateCoefficients": {
                        "defaultBySpecies": _defaults(),
                        "byMaterial": {"Si": {"unknown": 0.1}},
                    },
                },
            ),
            (
                "material empty name",
                {
                    **base,
                    "rateCoefficients": {
                        "defaultBySpecies": _defaults(),
                        "byMaterial": {"": {"ion": 0.1}},
                    },
                },
            ),
            (
                "material row type",
                {
                    **base,
                    "rateCoefficients": {
                        "defaultBySpecies": _defaults(),
                        "byMaterial": {"Si": [0.1]},
                    },
                },
            ),
            ("deposit without material", {**base, "operation": "deposit"}),
            ("etch with material", {**base, "depositMaterial": "SiO2"}),
        )
        for label, candidate in invalid:
            with self.subTest(step=label):
                with self.assertRaises(ValueError):
                    parse_step({}, candidate)

    # -- execution ---------------------------------------------------------

    def test_two_dimensional_ion_neutral_etch_matches_native_reference(self) -> None:
        project = _project(2)
        params = _params()
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
        self.assertEqual(metadata["nativeClass"], "ps.MultiParticleProcess")
        self.assertEqual(metadata["operation"], "etch")
        self.assertEqual(metadata["speciesOrder"], ["ion", "neutral"])
        self.assertEqual(metadata["depositMaterial"], None)
        self.assertEqual(metadata["rateFunction"]["kind"], "weighted-sum-table")
        self.assertEqual(metadata["rateFunction"]["operationSign"], -1.0)
        self.assertEqual(metadata["rateFunction"]["defaultBySpecies"], _defaults())
        self.assertEqual(metadata["rateFunction"]["byMaterial"], {})
        audit = metadata["rateCallback"]
        self.assertGreater(audit["count"], 0)
        self.assertEqual(audit["materials"], {"Si": audit["count"]})
        self.assertEqual(len(audit["fluxSums"]), 2)
        self.assertEqual(len(audit["fluxMaxima"]), 2)
        self.assertTrue(all(value > 0.0 for value in audit["fluxSums"]))
        self.assertLessEqual(len(audit["samples"]), 256)
        self.assertTrue(all(sample["returnedVelocity"] < 0.0 for sample in audit["samples"]))
        self.assertEqual(metadata["fluxEngine"], "CPU_DISK")
        self.assertEqual(metadata["dimension"], 2)
        self.assertEqual(metadata["gridDelta"], GRID_DELTA)
        self.assertEqual(metadata["levelSetCount"], 1)
        self.assertEqual(metadata["materialMap"], list(_material_order(candidate)))
        self.assertEqual(outcome.plan.identity_json()["processId"], "multiParticle")

    def test_cpu_triangle_matches_native_reference(self) -> None:
        project = _project(2)
        params = _params()
        reference = _plane(2)
        candidate = _plane(2)

        _reference(project, reference, params, kernel="CPU_TRIANGLE")
        outcome = self._run(
            params,
            candidate,
            project,
            compute=ComputePolicy(backend="cpu", kernel="CPU_TRIANGLE"),
        )

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertEqual(outcome.metadata["fluxEngine"], "CPU_TRIANGLE")
        self.assertEqual(outcome.plan.identity_json()["engineKernel"], "CPU_TRIANGLE")
        self.assertGreater(outcome.metadata["rateCallback"]["count"], 0)

    def test_material_override_coefficients_match_native_reference(self) -> None:
        project = _project(2)
        params = _params(
            rateCoefficients={
                "defaultBySpecies": _defaults(),
                "byMaterial": {"Si": {"ion": 0.4, "neutral": 0.0}},
            },
        )
        reference = _plane(2)
        candidate = _plane(2)

        _reference(project, reference, params)
        outcome = self._run(params, candidate, project)

        self._assert_parity(reference, outcome.state.geometry.handle)
        metadata = outcome.metadata
        self.assertEqual(
            metadata["rateFunction"]["byMaterial"],
            {"Si": {"ion": 0.4, "neutral": 0.0}},
        )
        samples = metadata["rateCallback"]["samples"]
        self.assertTrue(samples)
        for sample in samples:
            self.assertEqual(sample["material"], "Si")
            self.assertEqual(sample["coefficients"], {"ion": 0.4, "neutral": 0.0})

    def test_two_dimensional_deposit_matches_native_reference(self) -> None:
        project = _project(2)
        params = _params(operation="deposit", depositMaterial="SiO2")
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
        metadata = outcome.metadata
        self.assertEqual(metadata["operation"], "deposit")
        self.assertEqual(metadata["depositMaterial"], "SiO2")
        self.assertEqual(metadata["rateFunction"]["operationSign"], 1.0)
        self.assertTrue(all(sample["returnedVelocity"] > 0.0 for sample in metadata["rateCallback"]["samples"]))
        self.assertEqual(metadata["levelSetCount"], 2)
        self.assertEqual(metadata["materialMap"], list(order))

    def test_three_dimensional_smoke_matches_native_reference(self) -> None:
        project = _project(3)
        params = _params(duration=0.08)
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
        self.assertGreater(outcome.metadata["rateCallback"]["count"], 0)

    def test_project_fixed_seed_is_used_when_the_context_has_none(self) -> None:
        project = _project(2)
        params = _params()
        reference = _plane(2)
        candidate = _plane(2)

        _reference(project, reference, params, seed=RAY_SEED)
        outcome = self._run(params, candidate, project, seed=None)

        self._assert_parity(reference, outcome.state.geometry.handle)
        self.assertEqual(outcome.metadata["rayTracing"]["rngSeed"], RAY_SEED)
        self.assertFalse(outcome.metadata["rayTracing"]["useRandomSeeds"])

    # -- import isolation --------------------------------------------------

    def test_import_does_not_pull_in_the_worker_or_backend(self) -> None:
        code = (
            "import sys\n"
            "import plugins.multiParticle\n"
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
