"""Tests for the v2 voxel-probe surrogate plugin.

The v2 contract separates process identity, engine capability, state
representation, and the level-set <-> voxel conversion. These tests drive one
small 2D plane through the whole path:

    resolve_execution -> ExecutionPlan.run (adapter prepare -> variant.execute(engine) -> commit)

No server and no project data directory are used. The height-field guard case
runs in a subprocess because the underlying native adapter can crash instead of
raising.
"""

from __future__ import annotations

import dataclasses
import inspect
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np
import viennaps as ps
from pydantic import BaseModel, ValidationError

from process_models.v2.adapters import DefaultStateAdapter
from process_models.v2.contract import ProcessContext, VariantRegistration
from process_models.v2.engine import EngineCapabilities, EngineLease, StaticEngine, StaticEngineProvider
from process_models.v2.registry import ProcessRegistry, compute_implementation_hash
from process_models.v2.resolver import ExecutionRequest, ExecutionResolutionError, resolve_execution
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessState,
    StateDelta,
    StateMissingError,
    StepResult,
)
from process_models.v2.surrogate.voxel_probe import (
    ADAPTER,
    VoxelProbeParams,
    VoxelProbeProcessSpec,
    VoxelProbeVariant,
    register_voxel_probe,
)

GRID_DELTA = 0.1
X_EXTENT = 1.0
REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _configure(dimension: int = 2) -> None:
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.setDimension(dimension)


def _plane_domain() -> ps.Domain:
    domain = ps.Domain(GRID_DELTA, X_EXTENT, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _material_order(domain: ps.Domain) -> tuple[int, ...]:
    material_map = domain.getMaterialMap()
    return tuple(int(material_map.getMaterialIdAtIdx(index)) for index in range(material_map.size()))


def _state(domain: ps.Domain, dimension: int = 2) -> ProcessState:
    order = _material_order(domain)
    return ProcessState(
        geometry=LevelSetState(
            handle=domain,
            dimension=dimension,
            grid_delta=GRID_DELTA,
        ),
        materials=MaterialState(order=order, names={order[0]: "Si"}),
    )


def _provider() -> StaticEngineProvider:
    capabilities = EngineCapabilities(
        engine_id="python",
        version="3.12",
        kind="surrogate",
        backends=("cpu",),
        capabilities=(),
    )
    return StaticEngineProvider([StaticEngine(capabilities, handle=None, fingerprint="python-3.12")])


class _LeaseRecordingVariant(VoxelProbeVariant):
    """Voxel-probe variant that records the lease passed by ``plan.run()``."""

    variant_id = "voxel_probe_lease"

    def __init__(self) -> None:
        self.engines: list[EngineLease] = []

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: EngineLease,
    ) -> StepResult:
        self.engines.append(engine)
        return super().execute(state, params, ctx, engine)


def _surface_points(domain: ps.Domain) -> np.ndarray:
    mesh = domain.getSurfaceMesh()
    points = [[float(value) for value in node] for node in mesh.getNodes()]
    if not points:
        return np.zeros((0, 3), dtype=float)
    return np.asarray(points, dtype=float)


def _sorted_surface_points(domain: ps.Domain) -> np.ndarray:
    points = _surface_points(domain)
    if points.size == 0:
        return points
    return points[np.lexsort((points[:, 0], points[:, 1], points[:, 2]))]


def _surface_top(domain: ps.Domain, dimension: int = 2) -> float:
    points = _surface_points(domain)
    if points.size == 0:
        raise AssertionError("domain has no surface nodes")
    vertical = 1 if dimension == 2 else 2
    return float(np.max(points[:, vertical]))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
class VoxelProbeV2Test(unittest.TestCase):
    def setUp(self) -> None:
        _configure(2)
        self.registry = ProcessRegistry()
        self.registration = register_voxel_probe(self.registry)
        self.adapter = DefaultStateAdapter()
        self.variant = VoxelProbeVariant()

    # -- helpers -----------------------------------------------------------
    def _context(self, dimension: int = 2) -> ProcessContext:
        return ProcessContext(
            dimension=dimension,
            grid_delta=GRID_DELTA,
            project={},
            step={},
        )

    def _plan(self, state: ProcessState, params: VoxelProbeParams, dimension: int = 2):
        request = ExecutionRequest(
            process_id="voxel_probe",
            variant_id="voxel_probe",
            params=params,
            context=self._context(dimension),
            state=state,
        )
        return resolve_execution(request, self.registry, _provider(), self.adapter)

    def _run(self, state: ProcessState, params: VoxelProbeParams, dimension: int = 2):
        plan = self._plan(state, params, dimension)
        prepared = plan.state_adapter.prepare(state, plan.variant.capabilities)
        outcome = plan.run()
        return plan, prepared, outcome

    # -- schema ------------------------------------------------------------
    def test_params_schema_bounds_and_extra_keys(self) -> None:
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="melt")
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", layers=0)
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", layers=9)
        with self.assertRaises(ValidationError):
            VoxelProbeParams(operation="identity", thickness=1.0)

        self.assertEqual(VoxelProbeParams(operation="erode_top_layer", layers=8).layers, 8)

    def test_validate_reports_dimension_and_layer_overflow(self) -> None:
        valid = self.variant.validate(
            self._context(),
            VoxelProbeParams(operation="erode_top_layer", layers=8),
        )
        self.assertEqual(valid, [])

        # ProcessContext validates ``dimension`` in (2, 3); bypass the guard so
        # the resolver's own dimension check is the thing under test.
        bad_dimension = ProcessContext(dimension=2, grid_delta=GRID_DELTA)
        object.__setattr__(bad_dimension, "dimension", 4)
        issues = self.variant.validate(bad_dimension, VoxelProbeParams(operation="identity"))
        self.assertTrue(any(issue.path == "dimension" and issue.severity == "error" for issue in issues))

        overflow = VoxelProbeParams.model_construct(operation="erode_top_layer", layers=9)
        issues = self.variant.validate(self._context(), overflow)
        self.assertTrue(any(issue.path == "layers" and issue.severity == "error" for issue in issues))

    # -- registry / identity ----------------------------------------------
    def test_registry_resolves_process_and_variant_identity(self) -> None:
        spec = self.registry.get_process("voxel_probe")
        self.assertEqual(spec.id, "voxel_probe")
        self.assertEqual(spec.version, "1.0.0")
        self.assertEqual(spec, VoxelProbeProcessSpec)
        self.assertEqual(spec.operations, ("geometry",))
        self.assertEqual(spec.dimensions, (2, 3))
        self.assertIs(spec.params_model, VoxelProbeParams)

        resolved = self.registry.get_variant("voxel_probe", "voxel_probe")
        self.assertEqual(resolved.process_version, "1.0.0")
        self.assertEqual(resolved.variant_version, "1.0.0")
        self.assertIsInstance(resolved.variant.variant, VoxelProbeVariant)
        self.assertEqual(resolved.variant.capabilities, VoxelProbeVariant.capabilities)
        self.assertTrue(resolved.implementation_hash)
        self.assertEqual(
            resolved.implementation_hash, self.registry.get_variant("voxel_probe", "voxel_probe").implementation_hash
        )
        self.assertEqual(resolved.implementation_hash, compute_implementation_hash(resolved.variant.variant))

        # v2.1: the registration is (process_id, variant); everything else is
        # derived from the variant, and the registry computes the hash.
        self.assertEqual(self.registration.process_id, "voxel_probe")
        self.assertEqual(self.registration.variant_id, VoxelProbeVariant.variant_id)
        self.assertEqual(self.registration.version, VoxelProbeVariant.version)
        self.assertEqual(self.registration.capabilities, VoxelProbeVariant.capabilities)
        self.assertEqual(self.registration.engine, VoxelProbeVariant.engine)
        self.assertIsNone(self.registration.declared_hash)

        engine = self.registration.engine
        self.assertEqual(engine.kind, "surrogate")
        self.assertEqual(engine.engine_id, "python")
        self.assertEqual(engine.min_version, "3.12")

        descriptor = next(item for item in self.registry.list_processes() if item.id == "voxel_probe")
        self.assertEqual(descriptor.version, "1.0.0")
        self.assertEqual([variant.id for variant in descriptor.variants], ["voxel_probe"])
        self.assertTrue(descriptor.variants[0].experimental)

        plan = self._plan(_state(_plane_domain()), VoxelProbeParams(operation="identity"))
        identity = plan.identity()
        self.assertEqual(identity["process_id"], "voxel_probe")
        self.assertEqual(identity["variant_id"], "voxel_probe")
        self.assertEqual(identity["engine_id"], "python")
        self.assertEqual(identity["implementation_hash"], resolved.implementation_hash)

    # -- adapter prepare ---------------------------------------------------
    def test_adapter_prepare_materialises_voxels_from_geometry(self) -> None:
        domain = _plane_domain()
        state = _state(domain)
        self.assertIsNone(state.voxels)

        plan = self._plan(state, VoxelProbeParams(operation="identity"))
        prepared = plan.state_adapter.prepare(state, plan.variant.capabilities)

        self.assertIsNot(prepared, state)
        self.assertIsNone(state.voxels)
        self.assertIsNotNone(prepared.voxels)
        self.assertIs(prepared.geometry, state.geometry)

        grid = prepared.voxels.grid
        self.assertEqual(grid.material.ndim, 2)
        self.assertAlmostEqual(grid.spacing, GRID_DELTA)
        self.assertEqual(grid.material_order, _material_order(domain))
        self.assertGreater(int(np.count_nonzero(grid.material >= 0)), 0)
        self.assertEqual(prepared.voxels.source, "height-field-v1")

    def test_adapter_prepare_requires_geometry_when_voxels_are_missing(self) -> None:
        empty = ProcessState(materials=MaterialState(()))
        with self.assertRaises(StateMissingError):
            self.adapter.prepare(empty, VoxelProbeVariant.capabilities)

    # -- state immutability ------------------------------------------------
    def test_process_state_is_frozen_and_has_no_engine(self) -> None:
        state = _state(_plane_domain())
        self.assertFalse(hasattr(state, "engine"))
        self.assertFalse(hasattr(state, "meta"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.voxels = None

        copy = dataclasses.replace(state, materials=MaterialState(()))
        self.assertIsNot(copy, state)
        self.assertIs(copy.geometry, state.geometry)
        self.assertEqual(copy.materials.order, ())
        self.assertTrue(state.materials.order)

    # -- engine lease ------------------------------------------------------
    def test_plan_run_passes_the_engine_lease_explicitly(self) -> None:
        self.assertIn("engine", inspect.signature(VoxelProbeVariant.execute).parameters)

        registry = ProcessRegistry()
        register_voxel_probe(registry)
        recording = _LeaseRecordingVariant()
        registry.register_variant(VariantRegistration(process_id="voxel_probe", variant=recording))

        state = _state(_plane_domain())
        request = ExecutionRequest(
            process_id="voxel_probe",
            variant_id=recording.variant_id,
            params=VoxelProbeParams(operation="identity"),
            context=self._context(),
            state=state,
        )
        plan = resolve_execution(request, registry, _provider(), self.adapter)

        outcome = plan.run()

        self.assertEqual(len(recording.engines), 1)
        self.assertIs(recording.engines[0], plan.engine)
        self.assertIsInstance(recording.engines[0], EngineLease)
        self.assertEqual(recording.engines[0].engine_id, "python")
        self.assertIs(outcome.plan, plan)
        self.assertIsNotNone(outcome.delta.voxels)
        self.assertIsNone(outcome.state.voxels)

    # -- identity round trip ----------------------------------------------
    def test_identity_round_trip_commits_with_zero_surface_deviation(self) -> None:
        domain = _plane_domain()
        state = _state(domain)
        before = _sorted_surface_points(domain)
        self.assertGreater(before.size, 0)

        _, _, outcome = self._run(state, VoxelProbeParams(operation="identity"))

        delta = outcome.delta
        committed = outcome.state
        self.assertIsNotNone(delta.voxels)
        self.assertIsNone(delta.geometry)
        self.assertIsNotNone(committed.geometry)
        self.assertIsNone(committed.voxels)
        self.assertIsNot(committed.geometry.handle, domain)
        self.assertEqual(committed.geometry.dimension, 2)
        self.assertAlmostEqual(committed.geometry.grid_delta, GRID_DELTA)

        after = _sorted_surface_points(committed.geometry.handle)
        self.assertEqual(after.shape, before.shape)
        self.assertLessEqual(float(np.max(np.abs(after - before))), 1e-9)

        metadata = outcome.metadata
        self.assertEqual(metadata["operation"], "identity")
        self.assertEqual(metadata["layers"], 1)
        self.assertEqual(metadata["removedCells"], 0)
        self.assertEqual(metadata["adapter"], ADAPTER)
        self.assertEqual(metadata["voxelShape"], list(metadata["voxelShape"]))
        self.assertTrue(metadata["materialOrder"])
        self.assertTrue(outcome.warnings)

    def test_erode_top_layer_lowers_the_surface_by_about_one_grid_delta(self) -> None:
        domain = _plane_domain()
        state = _state(domain)
        top_before = _surface_top(domain)

        _, _, outcome = self._run(
            state,
            VoxelProbeParams(operation="erode_top_layer", layers=1),
        )

        metadata = outcome.metadata
        self.assertGreater(metadata["removedCells"], 0)
        self.assertEqual(metadata["operation"], "erode_top_layer")
        self.assertEqual(metadata["layers"], 1)
        self.assertEqual(metadata["adapter"], ADAPTER)

        top_after = _surface_top(outcome.state.geometry.handle)
        drop = top_before - top_after
        self.assertGreater(drop, 0.0)
        self.assertLessEqual(abs(drop - GRID_DELTA), 1.5 * GRID_DELTA)

        # The input domain is still the original plane.
        self.assertAlmostEqual(_surface_top(domain), top_before, places=12)

    def test_plugin_does_not_mutate_the_input_state(self) -> None:
        domain = _plane_domain()
        state = _state(domain)
        before = _sorted_surface_points(domain)
        level_sets_before = len(domain.getLevelSets())

        plan, prepared, outcome = self._run(
            state,
            VoxelProbeParams(operation="erode_top_layer", layers=1),
        )
        committed = outcome.state

        self.assertIsNone(state.voxels)
        self.assertIs(prepared.geometry, state.geometry)
        self.assertIsNot(prepared, state)
        self.assertIs(plan.state_adapter, self.adapter)
        self.assertIsNot(committed, prepared)
        self.assertIsNot(committed.geometry.handle, domain)

        np.testing.assert_array_equal(_sorted_surface_points(domain), before)
        self.assertEqual(len(domain.getLevelSets()), level_sets_before)
        self.assertEqual(committed.materials, state.materials)
        self.assertEqual(outcome.delta.voxels.grid.material_order, state.materials.order)

    # -- resolver rejections ----------------------------------------------
    def test_resolver_rejects_missing_state_and_wrong_dimension(self) -> None:
        empty = ProcessState(materials=MaterialState(()))
        with self.assertRaises(ExecutionResolutionError):
            self._plan(empty, VoxelProbeParams(operation="identity"))

        state = _state(_plane_domain())
        bad = ProcessContext(dimension=2, grid_delta=GRID_DELTA)
        object.__setattr__(bad, "dimension", 4)
        request = ExecutionRequest(
            process_id="voxel_probe",
            variant_id="voxel_probe",
            params=VoxelProbeParams(operation="identity"),
            context=bad,
            state=state,
        )
        with self.assertRaises(ExecutionResolutionError):
            resolve_execution(request, self.registry, _provider(), self.adapter)

    # -- determinism -------------------------------------------------------
    def test_execution_is_deterministic(self) -> None:
        domain = _plane_domain()
        params = VoxelProbeParams(operation="erode_top_layer", layers=1)

        first = self._run(_state(domain), params)
        second = self._run(_state(domain), params)

        first_points = _sorted_surface_points(first[2].state.geometry.handle)
        second_points = _sorted_surface_points(second[2].state.geometry.handle)
        np.testing.assert_array_equal(first_points, second_points)
        self.assertEqual(first[2].metadata, second[2].metadata)
        self.assertEqual(first[0].identity(), second[0].identity())

    # -- structured outputs -------------------------------------------------
    def test_step_result_carries_voxel_delta_and_empty_outputs(self) -> None:
        state = _state(_plane_domain())
        plan = self._plan(state, VoxelProbeParams(operation="erode_top_layer", layers=1))

        outcome = plan.run()

        result = outcome.result
        self.assertIsInstance(result, StepResult)
        self.assertIsInstance(result.delta, StateDelta)
        self.assertIsInstance(result.outputs, OutputSet)
        self.assertEqual(result.outputs, OutputSet())
        self.assertEqual(result.outputs.metrology, ())
        self.assertEqual(result.outputs.artifacts, ())

        # The voxel delta lives on ``result.delta``; report data does not.
        self.assertIsNotNone(result.delta.voxels)
        self.assertIsNone(result.delta.geometry)
        self.assertFalse(result.delta.is_empty())
        self.assertIs(outcome.delta, result.delta)
        self.assertIs(outcome.outputs, result.outputs)
        self.assertIs(outcome.metadata, result.metadata)
        self.assertIs(outcome.warnings, result.warnings)
        self.assertEqual(outcome.artifacts, [])

    # -- height-field guard ------------------------------------------------
    def test_height_field_fin_is_rejected_in_a_subprocess(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", _FIN_GUARD_SCRIPT],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=(
                "fin guard subprocess failed with return code "
                f"{completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            ),
        )


_FIN_GUARD_SCRIPT = """
import viennaps as ps

from process_models.v2.adapters import DefaultStateAdapter
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from process_models.v2.surrogate.voxel_probe import VoxelProbeVariant
from process_models.voxel import VoxelAdapterError

ps.setNumThreads(1)
ps.Length.setUnit("um")
ps.setDimension(2)
domain = ps.Domain(0.1, 1.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
ps.MakeFin(
    domain,
    finWidth=0.4,
    finHeight=0.3,
    finTaperAngle=0.0,
    maskHeight=0.2,
    maskTaperAngle=0.0,
    halfFin=False,
    material=ps.Material.Si,
    maskMaterial=ps.Material.Mask,
).apply()

material_map = domain.getMaterialMap()
order = tuple(int(material_map.getMaterialIdAtIdx(i)) for i in range(material_map.size()))
state = ProcessState(
    geometry=LevelSetState(handle=domain, dimension=2, grid_delta=0.1),
    materials=MaterialState(order=order),
)

try:
    DefaultStateAdapter().prepare(state, VoxelProbeVariant.capabilities)
except VoxelAdapterError:
    raise SystemExit(0)
raise SystemExit(3)
"""


if __name__ == "__main__":
    unittest.main()
