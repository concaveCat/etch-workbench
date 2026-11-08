"""Tests for the native v2 isotropic plugin.

Each test builds one small 2D/3D level-set domain, resolves the real
``isotropic``/``native`` registration against a static native engine whose
handle is the installed ``viennaps`` module, and executes the plan through
:meth:`ExecutionPlan.run` (or :func:`run_step`). No server, worker process, or
data directory is involved.
"""

from __future__ import annotations

import dataclasses
import hashlib
import unittest
from typing import Any

import numpy as np
import viennaps as ps
from pydantic import ValidationError

from plugins.isotropic import (
    IsotropicNativeVariant,
    IsotropicParams,
    IsotropicProcessSpec,
    register,
)
from process_models.v2.contract import ProcessContext, VariantCapabilities, VariantRegistration
from process_models.v2.engine import (
    EngineCapabilities,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import (
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionResolutionError,
    execute_plan,
    resolve_execution,
    run_step,
)
from process_models.v2.state import (
    CellSetState,
    GridSpec,
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessState,
    StateDelta,
    StepResult,
)

GRID_DELTA = 0.1
X_EXTENT = 1.0
FINGERPRINT = "test-native-fingerprint"
VIENNAPS_VERSION = "4.7.0"


def _engine_capabilities(version: str = VIENNAPS_VERSION) -> EngineCapabilities:
    return EngineCapabilities(
        engine_id="viennaps",
        version=version,
        kind="native",
        backends=("cpu",),
    )


def _provider(version: str = VIENNAPS_VERSION, handle: Any = ps) -> StaticEngineProvider:
    return StaticEngineProvider(
        [StaticEngine(capabilities=_engine_capabilities(version), handle=handle, fingerprint=FINGERPRINT)]
    )


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


def _masked_trench() -> Any:
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakeTrench(
        domain=domain,
        trenchWidth=0.4,
        trenchDepth=0.4,
        trenchTaperAngle=0.0,
        maskHeight=0.3,
        maskTaperAngle=0.0,
        halfTrench=False,
        material=ps.Material.Si,
        maskMaterial=ps.Material.Mask,
    ).apply()
    return domain


def _material_order(domain: Any) -> tuple[int, ...]:
    mapping = domain.getMaterialMap()
    return tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))


def _state(domain: Any, dimension: int = 2) -> ProcessState:
    order = _material_order(domain)
    return ProcessState(
        geometry=LevelSetState(
            handle=domain,
            dimension=dimension,
            grid_delta=GRID_DELTA,
        ),
        materials=MaterialState(order=order),
    )


def _context(
    dimension: int = 2,
    project: dict[str, Any] | None = None,
) -> ProcessContext:
    if project is None:
        project = {
            "schemaVersion": 3,
            "dimension": dimension,
            "geometry": {"kind": "plane", "substrateMaterial": "Si"},
            "solver": {"timeStepRatio": 0.25},
        }
    return ProcessContext(dimension=dimension, grid_delta=GRID_DELTA, project=project)


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


def _material_bounds(domain: Any, material_id: int) -> tuple[float, float] | None:
    """Return the (min, max) vertical extent of ``material_id`` surface cells."""
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in node] for node in mesh.getNodes()]
    lines = [[int(value) for value in row] for row in mesh.getLines()]
    data = mesh.getCellData()
    material_ids: list[int] = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    verticals = [
        points[node][1] for cell, material in zip(lines, material_ids) if material == material_id for node in cell
    ]
    if not verticals:
        return None
    return min(verticals), max(verticals)


def _surface_digest(domain: Any) -> str:
    return hashlib.sha256(_sorted_surface(domain).tobytes()).hexdigest()


class _RecordingHandle:
    """Module proxy that records attribute access.

    Used to prove the variant reads the ``viennaps`` module from the engine
    lease (``engine.handle``) instead of importing or reaching into the state.
    """

    def __init__(self, module: Any) -> None:
        self._module = module
        self.accessed: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.accessed.append(name)
        return getattr(self._module, name)


class IsotropicNativePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        self.registry = ProcessRegistry()
        self.registration = register(self.registry)

    def _params(self, **overrides: Any) -> IsotropicParams:
        values: dict[str, Any] = {"operation": "etch", "rateMagnitude": 0.3, "duration": 1.0}
        values.update(overrides)
        return IsotropicParams(**values)

    def _resolve(
        self,
        params: IsotropicParams,
        state: ProcessState,
        ctx: ProcessContext,
        version: str = VIENNAPS_VERSION,
        handle: Any = ps,
    ) -> Any:
        request = ExecutionRequest(
            process_id="isotropic",
            variant_id="native",
            params=params,
            context=ctx,
            state=state,
        )
        return resolve_execution(request, self.registry, _provider(version, handle))

    def _execute(
        self,
        params: IsotropicParams,
        domain: Any | None = None,
        dimension: int = 2,
        project: dict[str, Any] | None = None,
        ctx: ProcessContext | None = None,
        handle: Any = ps,
    ) -> tuple[ExecutionPlan, ExecutionOutcome, Any]:
        state = _state(domain if domain is not None else _plane(dimension), dimension)
        ctx = ctx or _context(dimension, project)
        plan = self._resolve(params, state, ctx, handle=handle)
        outcome = plan.run()
        return plan, outcome, outcome.state.geometry.handle

    # -- identity / registration ------------------------------------------

    def test_registration_exposes_stable_process_and_variant_identity(self) -> None:
        self.assertEqual(IsotropicProcessSpec.id, "isotropic")
        self.assertEqual(IsotropicProcessSpec.version, "1.0.0")
        self.assertEqual(IsotropicProcessSpec.params_model, IsotropicParams)
        self.assertEqual(IsotropicProcessSpec.dimensions, (2, 3))
        self.assertEqual(IsotropicProcessSpec.operations, ("etch", "deposit"))

        variant = IsotropicNativeVariant()
        self.assertEqual(variant.variant_id, "native")
        self.assertEqual(variant.version, "1.0.0")
        self.assertEqual(variant.capabilities.dimensions, (2, 3))
        self.assertEqual(variant.capabilities.reads, frozenset({"geometry", "materials"}))
        self.assertEqual(variant.capabilities.writes, frozenset({"geometry", "materials"}))
        self.assertTrue(variant.capabilities.supports_masks)
        self.assertTrue(variant.capabilities.mutates_input)
        self.assertEqual(variant.capabilities.determinism, "strict")
        self.assertEqual(variant.engine.kind, "native")
        self.assertEqual(variant.engine.engine_id, "viennaps")
        self.assertEqual(variant.engine.min_version, "4.6.2")

        self.assertEqual(self.registration.process_id, "isotropic")
        self.assertIsInstance(self.registration.variant, IsotropicNativeVariant)
        self.assertEqual(self.registration.variant_id, variant.variant_id)
        self.assertEqual(self.registration.version, variant.version)
        self.assertEqual(self.registration.capabilities, variant.capabilities)
        self.assertEqual(self.registration.engine, variant.engine)
        self.assertIsNone(self.registration.declared_hash)

        resolved = self.registry.get_variant("isotropic", "native")
        self.assertIsInstance(resolved.variant, VariantRegistration)
        self.assertIs(resolved.variant, self.registration)
        self.assertEqual(resolved.variant.capabilities, variant.capabilities)
        self.assertEqual(len(resolved.implementation_hash), 64)
        int(resolved.implementation_hash, 16)
        descriptors = self.registry.list_variants("isotropic")
        self.assertEqual([descriptor.id for descriptor in descriptors], ["native"])
        self.assertEqual(descriptors[0].kind, "native")
        self.assertEqual(descriptors[0].engine_id, "viennaps")

    def test_params_reject_invalid_values(self) -> None:
        params = self._params()
        self.assertEqual(params.rateMode, "uniform")
        self.assertEqual(params.depositMaterial, "SiO2")
        self.assertIsNone(params.isotropicTimeStepRatio)

        invalid = (
            {"operation": "isotropic"},
            {"rateMagnitude": 0.0},
            {"rateMagnitude": float("inf")},
            {"duration": 0.0},
            {"duration": float("nan")},
            {"isotropicTimeStepRatio": 0.01},
            {"isotropicTimeStepRatio": 0.6},
            {"materialRates": {"Si": -0.1}},
            {"materialRates": {"Si": float("inf")}},
            {"maskMaterials": [""]},
            {"unexpected": 1},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self._params(**overrides)

    def test_validate_reports_rate_mode_and_mask_issues(self) -> None:
        variant = IsotropicNativeVariant()
        ctx = _context()

        empty_table = variant.validate(
            ctx,
            self._params(rateMode="material", materialRates={}),
        )
        self.assertTrue(
            any(issue.path == "materialRates" and issue.severity == "error" for issue in empty_table),
            empty_table,
        )

        uniform_table = variant.validate(ctx, self._params(materialRates={"Si": 0.1}))
        self.assertTrue(
            any(issue.path == "materialRates" and issue.severity == "error" for issue in uniform_table),
            uniform_table,
        )

        masks_in_material_mode = variant.validate(
            ctx,
            self._params(rateMode="material", materialRates={"Si": 0.1}, maskMaterials=["Mask"]),
        )
        self.assertEqual(len(masks_in_material_mode), 1)
        self.assertEqual(masks_in_material_mode[0].severity, "warning")
        self.assertTrue(
            all(issue.severity != "error" for issue in masks_in_material_mode),
            "material-mode masks must be a warning so resolution still succeeds",
        )

        self.assertEqual(variant.validate(ctx, self._params()), [])

    # -- execution ---------------------------------------------------------

    def test_etch_lowers_the_surface_by_rate_times_duration(self) -> None:
        domain = _plane(2)
        before = _top(domain, 2)
        plan, outcome, executed_domain = self._execute(self._params(), domain=domain)
        delta = outcome.delta

        self.assertIs(executed_domain, domain)
        self.assertIsInstance(outcome.result, StepResult)
        self.assertIsInstance(delta, StateDelta)
        self.assertIs(delta, outcome.result.delta)
        self.assertIsInstance(delta.geometry, LevelSetState)
        self.assertIs(delta.geometry.handle, domain)
        self.assertEqual(delta.geometry.dimension, 2)
        self.assertEqual(delta.geometry.grid_delta, GRID_DELTA)
        self.assertIsInstance(delta.materials, MaterialState)
        self.assertEqual(delta.materials.order, (int(ps.Material.Si),))
        self.assertIs(outcome.state.geometry.handle, domain)
        self.assertIs(outcome.state.geometry, delta.geometry)
        self.assertEqual(outcome.state.materials, delta.materials)
        self.assertIs(outcome.plan, plan)

        after = _top(domain, 2)
        self.assertAlmostEqual(after - before, -0.3, delta=1e-6)

        self.assertIs(outcome.metadata, outcome.result.metadata)
        self.assertEqual(outcome.metadata["operation"], "etch")
        self.assertEqual(outcome.metadata["rateMode"], "uniform")
        self.assertEqual(outcome.metadata["requestedRate"], 0.3)
        self.assertEqual(outcome.metadata["actualRate"], 0.3)
        self.assertAlmostEqual(outcome.metadata["signedRate"], -0.3)
        self.assertEqual(outcome.metadata["duration"], 1.0)
        self.assertEqual(outcome.metadata["nativeClass"], "ps.IsotropicProcess")
        self.assertEqual(outcome.metadata["fluxEngineType"], "CPU_DISK")
        self.assertEqual(outcome.metadata["dimension"], 2)
        self.assertEqual(outcome.metadata["timeStepRatio"], 0.25)
        self.assertEqual(outcome.metadata["maskMaterials"], [])
        self.assertEqual(plan.engine.engine_id, "viennaps")

    def test_step_result_keeps_outputs_empty_and_delta_has_geometry_and_materials(self) -> None:
        domain = _plane(2)
        _plan, outcome, _domain = self._execute(self._params(), domain=domain)
        result = outcome.result

        self.assertIsInstance(result, StepResult)
        self.assertIsInstance(result.outputs, OutputSet)
        self.assertEqual(result.outputs, OutputSet())
        self.assertEqual(result.outputs.metrology, ())
        self.assertEqual(result.outputs.artifacts, ())
        self.assertIs(outcome.outputs, result.outputs)
        self.assertEqual(outcome.artifacts, [])

        # Report data lives on the result, never in the state delta.
        self.assertIsInstance(result.delta, StateDelta)
        self.assertFalse(hasattr(result.delta, "metadata"))
        self.assertFalse(hasattr(result.delta, "warnings"))
        self.assertIs(result.delta.geometry.handle, domain)
        self.assertEqual(result.delta.materials.order, (int(ps.Material.Si),))
        self.assertIsNone(result.delta.layers)
        self.assertIsNone(result.delta.fields)
        self.assertIsNone(result.delta.voxels)
        self.assertIs(outcome.state.geometry, result.delta.geometry)
        self.assertIs(outcome.state.materials, result.delta.materials)
        self.assertIs(outcome.warnings, result.warnings)
        self.assertEqual(result.warnings, [])

    def test_step_result_carries_warnings_outside_the_state(self) -> None:
        params = self._params(
            rateMagnitude=0.2,
            duration=0.2,
            rateMode="material",
            materialRates={"Si": 0.2},
            maskMaterials=["Mask"],
        )
        _plan, outcome, _domain = self._execute(params, domain=_masked_trench())

        self.assertIs(outcome.warnings, outcome.result.warnings)
        self.assertTrue(outcome.warnings)
        self.assertIn("maskMaterials", outcome.warnings[0])
        self.assertIsNone(outcome.result.delta.layers)

    def test_deposit_raises_the_surface_and_adds_sio2(self) -> None:
        domain = _plane(2)
        before = _top(domain, 2)
        params = self._params(operation="deposit", depositMaterial="SiO2")
        plan, outcome, _domain = self._execute(params, domain=domain)
        delta = outcome.delta

        after = _top(domain, 2)
        self.assertAlmostEqual(after - before, 0.3, delta=1e-6)

        order = _material_order(domain)
        self.assertEqual(order, (int(ps.Material.Si), int(ps.Material.SiO2)))
        self.assertEqual(delta.materials.order, order)
        self.assertEqual(outcome.state.materials.order, order)
        self.assertEqual(outcome.metadata["depositMaterial"], "SiO2")
        self.assertEqual(outcome.metadata["levelSetCount"], 2)
        self.assertEqual(outcome.metadata["materialMap"], list(order))

        # The deposited material is the new top level set, as in the worker path.
        self.assertEqual(domain.getNumberOfLevelSets(), 2)
        self.assertEqual(order[-1], int(ps.Material.SiO2))
        bounds = _material_bounds(domain, int(ps.Material.SiO2))
        self.assertIsNotNone(bounds)
        self.assertGreater(bounds[1], 0.2)

        # The plan's request state keeps its original order; the variant's
        # materials delta updates the committed state.
        self.assertEqual(plan.request.state.materials.order, (int(ps.Material.Si),))
        self.assertEqual(outcome.state.materials.order, order)

    def test_material_rate_mode_moves_only_the_listed_material(self) -> None:
        domain = _masked_trench()
        mask_before = _material_bounds(domain, int(ps.Material.Mask))
        si_before = _material_bounds(domain, int(ps.Material.Si))
        self.assertIsNotNone(mask_before)
        self.assertIsNotNone(si_before)

        params = self._params(
            rateMagnitude=0.2,
            duration=0.3,
            rateMode="material",
            materialRates={"Si": 0.2},
        )
        _plan, outcome, _domain = self._execute(
            params,
            domain=domain,
            project={
                "schemaVersion": 3,
                "geometry": {"kind": "trench", "maskHeight": 0.3, "maskMaterial": "Mask"},
                "solver": {"timeStepRatio": 0.25},
            },
        )
        delta = outcome.delta
        self.assertIs(delta.geometry.handle, domain)

        mask_after = _material_bounds(domain, int(ps.Material.Mask))
        si_after = _material_bounds(domain, int(ps.Material.Si))
        self.assertIsNotNone(mask_after)
        self.assertIsNotNone(si_after)

        self.assertAlmostEqual(mask_after[1], mask_before[1], delta=1e-6)
        self.assertAlmostEqual(si_after[0] - si_before[0], -0.2 * 0.3, delta=1e-6)
        self.assertEqual(outcome.metadata["rateMode"], "material")
        self.assertEqual(outcome.metadata["actualRate"], 0.2)
        self.assertEqual(outcome.metadata["nativeDefaultRate"], 0.0)
        self.assertAlmostEqual(outcome.metadata["nativeMaterialRates"]["Si"], -0.2)
        self.assertEqual(outcome.metadata["maskMaterials"], [])

    def test_uniform_masks_come_from_the_project_geometry(self) -> None:
        domain = _masked_trench()
        mask_before = _material_bounds(domain, int(ps.Material.Mask))
        si_before = _material_bounds(domain, int(ps.Material.Si))
        self.assertIsNotNone(mask_before)
        self.assertIsNotNone(si_before)

        _plan, outcome, _domain = self._execute(
            self._params(rateMagnitude=0.2, duration=0.3),
            domain=domain,
            project={
                "schemaVersion": 3,
                "geometry": {"kind": "trench", "maskHeight": 0.3, "maskMaterial": "Mask"},
                "solver": {"timeStepRatio": 0.25},
            },
        )
        delta = outcome.delta
        self.assertEqual(delta.materials.order, _material_order(domain))

        mask_after = _material_bounds(domain, int(ps.Material.Mask))
        si_after = _material_bounds(domain, int(ps.Material.Si))
        self.assertIsNotNone(mask_after)
        self.assertIsNotNone(si_after)
        self.assertEqual(outcome.metadata["maskMaterials"], ["Mask"])
        self.assertAlmostEqual(mask_after[1], mask_before[1], delta=1e-6)
        self.assertAlmostEqual(si_after[0] - si_before[0], -0.2 * 0.3, delta=1e-6)

    def test_three_dimensional_plane_smoke(self) -> None:
        domain = _plane(3)
        before = _top(domain, 3)
        _plan, outcome, _domain = self._execute(
            self._params(rateMagnitude=0.2, duration=0.5),
            domain=domain,
            dimension=3,
            ctx=_context(3),
        )
        after = _top(domain, 3)
        self.assertAlmostEqual(after - before, -0.1, delta=1e-6)
        self.assertEqual(outcome.delta.geometry.dimension, 3)
        self.assertEqual(outcome.metadata["dimension"], 3)
        self.assertEqual(outcome.state.geometry.dimension, 3)

    # -- resolver admission ------------------------------------------------

    def test_missing_geometry_is_rejected(self) -> None:
        state = ProcessState(
            materials=MaterialState(order=(int(ps.Material.Si),)),
        )
        with self.assertRaises(ExecutionResolutionError) as raised:
            self._resolve(self._params(), state, _context())
        self.assertIn("geometry", str(raised.exception))

    def test_engine_version_mismatch_is_rejected_without_fallback(self) -> None:
        state = _state(_plane(2))
        with self.assertRaises(ExecutionResolutionError) as raised:
            self._resolve(self._params(), state, _context(), version="4.6.0")
        message = str(raised.exception)
        self.assertIn("viennaps", message)
        self.assertIn("4.6.2", message)
        self.assertIn("4.6.0", message)

        # A matching engine next to the mismatched one still resolves; there is
        # no fallback when only the mismatched engine exists (above).
        provider = StaticEngineProvider(
            [
                StaticEngine(capabilities=_engine_capabilities("4.6.0"), handle=ps, fingerprint="old"),
                StaticEngine(capabilities=_engine_capabilities(VIENNAPS_VERSION), handle=ps, fingerprint=FINGERPRINT),
            ]
        )
        plan = resolve_execution(
            ExecutionRequest(
                process_id="isotropic",
                variant_id="native",
                params=self._params(),
                context=_context(),
                state=state,
            ),
            self.registry,
            provider,
        )
        self.assertEqual(plan.engine.engine_version, VIENNAPS_VERSION)
        self.assertEqual(plan.engine_fingerprint, FINGERPRINT)

    def test_wrong_dimension_is_rejected_by_the_resolver(self) -> None:
        class TwoDimensionalIsotropic(IsotropicNativeVariant):
            variant_id = "native-2d"
            capabilities = VariantCapabilities(
                dimensions=(2,),
                reads=frozenset({"geometry"}),
                writes=frozenset({"geometry"}),
                supports_masks=True,
                mutates_input=True,
            )

        variant = TwoDimensionalIsotropic()
        self.registry.register_variant(VariantRegistration(process_id="isotropic", variant=variant))

        domain = _plane(3)
        state = _state(domain, 3)
        request = ExecutionRequest(
            process_id="isotropic",
            variant_id="native-2d",
            params=self._params(),
            context=_context(3),
            state=state,
        )
        with self.assertRaises(ExecutionResolutionError) as raised:
            resolve_execution(request, self.registry, _provider())
        self.assertIn("dimension", str(raised.exception))

    def test_validation_errors_block_resolution(self) -> None:
        state = _state(_plane(2))
        request = ExecutionRequest(
            process_id="isotropic",
            variant_id="native",
            params=self._params(rateMode="material", materialRates={}),
            context=_context(),
            state=state,
        )
        with self.assertRaises(ExecutionResolutionError) as raised:
            resolve_execution(request, self.registry, _provider())
        self.assertIn("materialRates", str(raised.exception))

    # -- state / engine boundary ------------------------------------------

    def test_process_state_is_frozen_and_carries_no_engine(self) -> None:
        state = _state(_plane(2))
        self.assertFalse(hasattr(state, "engine"))
        self.assertFalse(hasattr(state, "meta"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.engine = None  # type: ignore[misc]

        grid = GridSpec(dimension=2, origin=(0.0, 0.0), spacing=GRID_DELTA, shape=(4, 4))
        cell_set = CellSetState(grid=grid, material_ids=np.zeros((4, 4), dtype=np.int32))
        copied = dataclasses.replace(state, fields=cell_set)
        self.assertIsNot(copied, state)
        self.assertIsNone(state.fields)
        self.assertIs(copied.fields, cell_set)
        self.assertIs(copied.geometry, state.geometry)

    def test_execute_requires_a_complete_project(self) -> None:
        state = _state(_plane(2))
        plan = self._resolve(self._params(), state, _context(2, project={}))

        with self.assertRaises(RuntimeError) as raised:
            plan.run()

        message = str(raised.exception)
        self.assertIn("schemaVersion", message)
        self.assertIn("geometry", message)
        self.assertIn("solver", message)

    def test_variant_uses_the_engine_lease_handle_explicitly(self) -> None:
        domain = _plane(2)
        before = _top(domain, 2)
        state = _state(domain)
        handle = _RecordingHandle(ps)
        plan = self._resolve(self._params(), state, _context(), handle=handle)

        self.assertIs(plan.engine.handle, handle)
        self.assertFalse(hasattr(state, "engine"))
        outcome = plan.run()

        # The variant read the module from the lease, not from a global import.
        self.assertIn("IsotropicProcess", handle.accessed)
        self.assertIn("Process", handle.accessed)
        self.assertEqual(outcome.metadata["nativeClass"], "ps.IsotropicProcess")
        self.assertAlmostEqual(_top(domain, 2) - before, -0.3, delta=1e-6)

    # -- identity / determinism --------------------------------------------

    def test_plan_identity_is_stable_and_complete(self) -> None:
        domain = _plane(2)
        state = _state(domain)
        ctx = _context()
        params = self._params()
        first = self._resolve(params, state, ctx)
        second = self._resolve(params, state, ctx)

        identity = first.identity()
        self.assertIs(first.request.state, state)
        self.assertIs(first.request.params, params)
        self.assertIs(first.request.context, ctx)
        self.assertEqual(identity, second.identity())
        self.assertEqual(
            set(identity),
            {
                "process_id",
                "process_version",
                "variant_id",
                "variant_version",
                "implementation_hash",
                "engine_id",
                "engine_version",
                "engine_fingerprint",
                "adapter",
                "engine_backend",
                "engine_kernel",
                "engine_device",
                "engine_strategy",
                "state_backend_id",
                "state_backend_version",
                "state_contract",
            },
        )
        self.assertEqual(identity["process_id"], "isotropic")
        self.assertEqual(identity["process_version"], "1.0.0")
        self.assertEqual(identity["variant_id"], "native")
        self.assertEqual(identity["variant_version"], "1.0.0")
        self.assertEqual(identity["engine_id"], "viennaps")
        self.assertEqual(identity["engine_version"], VIENNAPS_VERSION)
        self.assertEqual(identity["engine_fingerprint"], FINGERPRINT)
        self.assertEqual(identity["adapter"], "default-v2")
        self.assertEqual(len(identity["implementation_hash"]), 64)
        int(identity["implementation_hash"], 16)

    def test_execution_is_deterministic_on_fresh_domains(self) -> None:
        params = self._params(rateMagnitude=0.25, duration=0.4)
        ctx = _context()

        # ``execute_plan(plan)`` and ``plan.run()`` are the same entry point.
        first_domain = _plane(2)
        first_plan = self._resolve(params, _state(first_domain), ctx)
        first_outcome = execute_plan(first_plan)

        second_domain = _plane(2)
        second_plan = self._resolve(params, _state(second_domain), ctx)
        second_outcome = second_plan.run()

        first_surface = _sorted_surface(first_domain)
        second_surface = _sorted_surface(second_domain)
        np.testing.assert_array_equal(first_surface, second_surface)
        self.assertEqual(_surface_digest(first_domain), _surface_digest(second_domain))
        self.assertEqual(
            first_outcome.delta.materials,
            second_outcome.delta.materials,
        )
        self.assertEqual(first_outcome.metadata, second_outcome.metadata)
        self.assertEqual(first_outcome.state.materials, second_outcome.state.materials)
        self.assertEqual(first_plan.identity(), second_plan.identity())

    def test_run_step_matches_resolve_and_run(self) -> None:
        params = self._params(rateMagnitude=0.25, duration=0.4)
        ctx = _context()

        planned_domain = _plane(2)
        planned = self._resolve(params, _state(planned_domain), ctx).run()

        stepped_domain = _plane(2)
        stepped = run_step(
            self.registry,
            _provider(),
            "isotropic",
            "native",
            params,
            ctx,
            _state(stepped_domain),
        )

        np.testing.assert_array_equal(_sorted_surface(planned_domain), _sorted_surface(stepped_domain))
        self.assertEqual(planned.metadata, stepped.metadata)
        self.assertEqual(planned.delta.materials, stepped.delta.materials)
        self.assertEqual(planned.state.materials, stepped.state.materials)
        self.assertEqual(planned.plan.identity(), stepped.plan.identity())


if __name__ == "__main__":
    unittest.main()
