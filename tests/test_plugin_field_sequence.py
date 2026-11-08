"""End-to-end tests for a contiguous implant -> anneal -> analysis sequence.

The v1 worker implementation is used as the native reference. The v2 field
plugins must stamp the same cell-set fields and the executor must persist the
dense arrays only once, at the sequence boundary.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from typing import Any

import numpy as np
import viennaps as ps
from pydantic import BaseModel

from plugins.analysis import register as register_analysis
from plugins.anneal import register as register_anneal
from plugins.implant import register as register_implant
from process_models.v2.contract import ProcessContext, ProcessSpec, VariantCapabilities
from process_models.v2.engine import (
    EngineCapabilities,
    EngineRequirement,
    ExecutionProfile,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.engines.viennaps import ViennaPSMaterialRegistry
from process_models.v2.engines.viennaps_cellset import ViennaPSCellSetBackend
from process_models.v2.executor import FlowExecutor, FlowStep
from process_models.v2.geometry import register_geometry
from process_models.v2.registry import ProcessRegistry
from process_models.v2.state import (
    LevelSetState,
    MaterialState,
    ProcessState,
    StateDelta,
    StepResult,
)
from process_models.v2.state_backend import HandleStateBackend
from process_models.v2.storage import StateStore

ROOT = Path(__file__).resolve().parents[1]
GRID_DELTA = 0.02
X_EXTENT = 0.4
RUN_ID = "run-field-0001"


def _project() -> dict[str, Any]:
    return {
        "schemaVersion": 7,
        "dimension": 2,
        "domain": {"gridDelta": GRID_DELTA, "xExtent": X_EXTENT, "xBoundary": "reflective"},
        "solver": {"timeStepRatio": 0.25, "advection": {}},
    }


def _project3d() -> dict[str, Any]:
    return {
        "schemaVersion": 7,
        "dimension": 3,
        "domain": {
            "gridDelta": 0.05,
            "xExtent": 0.4,
            "yExtent": 0.4,
            "boundaryXY": "reflective",
        },
        "solver": {"timeStepRatio": 0.25, "advection": {}},
    }


def _plane3d(*, height: float = 0.0) -> Any:
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        0.05,
        0.4,
        0.4,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=height, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _plane(*, height: float = 0.0) -> Any:
    ps.setDimension(2)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=height, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _cell_set() -> dict[str, Any]:
    return {
        "height": 0.2,
        "substrateDepth": 0.1,
        "coverMaterial": "GAS",
        "periodic": False,
        "format": "json",
    }


def _steps() -> list[dict[str, Any]]:
    implant = {
        "id": "implant_step_01",
        "name": "P implant",
        "enabled": True,
        "model": "implant",
        "species": "P",
        "material": "Si",
        "energyKeV": 10.0,
        "doseCm2": 1.0e15,
        "tiltDeg": 7.0,
        "rotationDeg": 0.0,
        "doseControl": "WaferDose",
        "profile": {
            "source": "manual",
            "kind": "pearsonIV",
            "head": {"mu": 0.03, "sigma": 0.012, "beta": 1.15, "gamma": 6.2},
            "lateralMu": 0.0,
            "lateralSigma": 0.005,
        },
        "maskMaterials": [],
        "screenMaterials": ["SiO2"],
        "damage": {
            "kind": "hobler",
            "projectedRange": 0.027,
            "verticalSigma": 0.018,
            "lambdaValue": 0.024,
            "defectsPerIon": 142.9,
            "lateralSigma": 0.013,
            "lateralDeltaSigma": -0.02,
        },
        "labels": {
            "concentration": "P_total",
            "damage": "P_damage",
            "lastDamage": "P_damage_last",
        },
        "cellSet": _cell_set(),
    }
    anneal = {
        "id": "anneal_step_01",
        "name": "anneal",
        "enabled": True,
        "model": "anneal",
        "mode": "GaussSeidel",
        "schedule": {"kind": "isothermal", "temperatureK": 1273.15, "durationSeconds": 1.0},
        "diffusivity": {"kind": "arrhenius", "D0": 1.23e13, "Ea_eV": 3.566},
        "diffusionMaterials": ["Si"],
        "blockingMaterials": [],
        "speciesLabel": "P_total",
        "activeLabel": "P_active",
        "solidActivation": {"solubilityC0": 1.521e2, "solubilityEa_eV": 0.7102},
        "cellSet": _cell_set(),
    }
    analysis = {
        "id": "analysis_step_01",
        "name": "analysis",
        "enabled": True,
        "model": "analysis",
        "netDoping": {
            "donorLabels": ["P_active"],
            "acceptorLabels": [],
            "outputLabel": "net_doping",
        },
        "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
        "cellSet": _cell_set(),
    }
    return [implant, anneal, analysis]


def _material_order(domain: Any) -> tuple[int, ...]:
    mapping = domain.getMaterialMap()
    return tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))


def _state(domain: Any, *, dimension: int = 2, grid_delta: float = GRID_DELTA) -> ProcessState:
    return ProcessState(
        geometry=LevelSetState(handle=domain, dimension=dimension, grid_delta=grid_delta),
        materials=MaterialState(
            order=_material_order(domain),
            names={int(ps.Material.Si): "Si"},
        ),
    )


class _NoCheckpointBackend(HandleStateBackend):
    """Fail loudly if a field step tries to checkpoint the level-set handle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.checkpoints = 0

    def checkpoint(self, state: ProcessState) -> ProcessState:
        self.checkpoints += 1
        raise AssertionError("field steps must not checkpoint or copy the canonical geometry")


class _CountingCellSetBackend(ViennaPSCellSetBackend):
    """Count sequence-boundary conversions to prove they are not per-step."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ensures = 0
        self.snapshots = 0
        self.generates = 0

    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        self.generates += 1
        return super()._generate(*args, **kwargs)

    def ensure(self, *args: Any, **kwargs: Any) -> Any:
        self.ensures += 1
        return super().ensure(*args, **kwargs)

    def snapshot(self, *args: Any, **kwargs: Any) -> Any:
        self.snapshots += 1
        return super().snapshot(*args, **kwargs)


class _MoveParams(BaseModel):
    pass


class _MoveVariant:
    """Test-only process that replaces the canonical geometry handle."""

    variant_id = "native"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2,),
        reads=frozenset({"geometry"}),
        writes=frozenset({"geometry"}),
    )
    engine = EngineRequirement(
        kind="native",
        engine_id="viennaps",
        min_version="4.7.0",
        profiles=(ExecutionProfile(backend="cpu", kernel="CPU_DISK"),),
    )
    implementation_hash = None

    def __init__(self, replacement: Any) -> None:
        self._replacement = replacement

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Any]:
        del ctx, params
        return []

    def execute(
        self,
        state: ProcessState,
        params: BaseModel,
        ctx: ProcessContext,
        engine: Any,
    ) -> StepResult:
        del params, ctx, engine
        geometry = state.require("geometry")
        return StepResult(
            delta=StateDelta(
                geometry=LevelSetState(
                    handle=self._replacement,
                    dimension=geometry.dimension,
                    grid_delta=geometry.grid_delta,
                )
            )
        )


def _registry() -> ProcessRegistry:
    registry = ProcessRegistry()
    register_implant(registry)
    register_anneal(registry)
    register_analysis(registry)
    register_geometry(registry)
    return registry


def _provider(
    state_backend: HandleStateBackend | None = None,
    cellset_backend: Any | None = None,
) -> StaticEngineProvider:
    if state_backend is None:
        state_backend = _NoCheckpointBackend(
            ps,
            id="viennaps",
            version=str(ps.__version__),
            material_registry=ViennaPSMaterialRegistry(ps),
        )
    return StaticEngineProvider(
        [
            StaticEngine(
                capabilities=EngineCapabilities(
                    engine_id="viennaps",
                    version=str(ps.__version__),
                    kind="native",
                    backends=("cpu",),
                    kernels=("CPU_DISK",),
                ),
                handle=ps,
                fingerprint="field-sequence-test",
                state_backend=state_backend,
                cellset_backend=cellset_backend or ViennaPSCellSetBackend(ps),
            )
        ]
    )


def _normal_backend() -> HandleStateBackend:
    return HandleStateBackend(
        ps,
        id="viennaps",
        version=str(ps.__version__),
        material_registry=ViennaPSMaterialRegistry(ps),
    )


def _flow_steps(registry: ProcessRegistry, project: dict[str, Any], raw_steps: list[dict]) -> list[FlowStep]:
    steps: list[FlowStep] = []
    for raw in raw_steps:
        spec, params = registry.resolve_step(project, raw)
        steps.append(
            FlowStep(
                step_id=raw["id"],
                process_id=spec.id,
                variant_id="native",
                params=params,
                context=ProcessContext(
                    dimension=int(project["dimension"]),
                    grid_delta=float(project["domain"]["gridDelta"]),
                    project=project,
                    step=raw,
                ),
            )
        )
    return steps


def _v1_reference(
    project: dict[str, Any],
    raw_steps: list[dict],
    work_dir: Path,
    *,
    domain: Any | None = None,
) -> Any:
    """Run the production worker field functions on the same native domain."""
    from worker import simulate

    domain = domain or _plane()
    state: dict[str, Any] = {}
    functions = {
        "implant": simulate.apply_implant_step,
        "anneal": simulate.apply_anneal_step,
        "analysis": simulate.apply_analysis_step,
    }
    for raw in raw_steps:
        step = {**raw, "cellSet": {**raw["cellSet"], "format": "json"}}
        functions[step["model"]](
            ps,
            domain,
            project,
            step,
            run_dir=work_dir,
            run_id=RUN_ID,
            state=state,
        )
    return state["cellSet"]


class FieldSequenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work_dir = ROOT / "var" / f"test-field-sequence-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        self.project = _project()
        self.raw_steps = _steps()
        self.registry = _registry()

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_sequence_matches_the_native_worker_reference(self) -> None:
        reference = _v1_reference(self.project, self.raw_steps, self.work_dir / "reference")
        backend = _NoCheckpointBackend(
            ps,
            id="viennaps",
            version=str(ps.__version__),
            material_registry=ViennaPSMaterialRegistry(ps),
        )
        run = FlowExecutor(self.registry, _provider(backend), StateStore(self.work_dir / "v2")).run(
            _state(_plane()),
            _flow_steps(self.registry, self.project, self.raw_steps),
        )

        self.assertEqual(backend.checkpoints, 0)
        self.assertEqual([outcome.plan.process.id for outcome in run.outcomes], ["implant", "anneal", "analysis"])
        final = run.state.fields
        self.assertIsNotNone(final)
        assert final is not None
        self.assertIsNotNone(final.handle)

        expected_labels = {"P_total", "P_damage", "P_damage_last", "P_active", "net_doping"}
        self.assertTrue(expected_labels <= set(final.fields))
        for label in expected_labels:
            native_values = np.asarray(reference.getScalarData(label), dtype=np.float64)
            np.testing.assert_array_equal(
                final.fields[label].data.reshape(-1, order="F"),
                native_values,
                err_msg=label,
            )
        np.testing.assert_array_equal(
            final.material_ids.reshape(-1, order="F"),
            np.asarray(reference.getScalarData("Material"), dtype=np.int32),
        )

    def test_dense_arrays_are_persisted_only_at_the_sequence_boundary(self) -> None:
        run = FlowExecutor(
            self.registry,
            _provider(
                _NoCheckpointBackend(
                    ps,
                    id="viennaps",
                    version=str(ps.__version__),
                    material_registry=ViennaPSMaterialRegistry(ps),
                )
            ),
            StateStore(self.work_dir / "v2"),
        ).run(_state(_plane()), _flow_steps(self.registry, self.project, self.raw_steps))

        for step_id in ("implant_step_01", "anneal_step_01"):
            step_dir = self.work_dir / "v2" / "steps" / step_id
            payload = json.loads((step_dir / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["fieldsOmitted"])
            self.assertIsNotNone(payload["fields"])
            self.assertTrue(payload["fields"]["arraysOmitted"])
            self.assertFalse((step_dir / "fields.npz").exists())
            self.assertFalse((step_dir / "geometry.vpsd").exists())

        boundary = self.work_dir / "v2" / "steps" / "analysis_step_01"
        payload = json.loads((boundary / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("fieldsOmitted", payload)
        self.assertIsNotNone(payload["fields"])
        self.assertTrue((boundary / "fields.npz").exists())
        self.assertTrue((boundary / "geometry.vpsd").exists())

        # Every step still has an auditable plan and output record.
        for step_id in ("implant_step_01", "anneal_step_01", "analysis_step_01"):
            self.assertTrue((self.work_dir / "v2" / "steps" / step_id / "plan.json").exists())
            self.assertTrue((self.work_dir / "v2" / "steps" / step_id / "outputs.json").exists())

        self.assertEqual(len(run.outcomes[-1].outputs.metrology), 1)
        self.assertEqual(run.outcomes[-1].outputs.metrology[0].metric, "sheetResistance")

    def test_boundary_snapshot_rehydrates_for_a_later_field_step(self) -> None:
        store = StateStore(self.work_dir / "v2")
        domain = _plane()
        run = FlowExecutor(
            self.registry,
            _provider(
                _NoCheckpointBackend(
                    ps,
                    id="viennaps",
                    version=str(ps.__version__),
                    material_registry=ViennaPSMaterialRegistry(ps),
                )
            ),
            store,
        ).run(_state(domain), _flow_steps(self.registry, self.project, self.raw_steps))
        boundary = run.state.fields
        assert boundary is not None

        loaded = store.load_state("analysis_step_01", geometry_loader=lambda _ref: domain)
        self.assertIsNotNone(loaded.fields)
        assert loaded.fields is not None
        self.assertIsNone(loaded.fields.handle)

        follow_up = {
            "id": "analysis_step_02",
            "name": "analysis again",
            "enabled": True,
            "model": "analysis",
            "netDoping": {
                "donorLabels": ["P_active"],
                "acceptorLabels": [],
                "outputLabel": "net_doping",
            },
            "sheetResistance": [{"concentrationLabel": "P_active", "carrier": "electron"}],
            "cellSet": _cell_set(),
        }
        second = FlowExecutor(
            self.registry,
            _provider(
                _NoCheckpointBackend(
                    ps,
                    id="viennaps",
                    version=str(ps.__version__),
                    material_registry=ViennaPSMaterialRegistry(ps),
                )
            ),
            StateStore(self.work_dir / "v2-rehydrated"),
        ).run(loaded, _flow_steps(self.registry, self.project, [follow_up]))

        final = second.state.fields
        self.assertIsNotNone(final)
        assert final is not None
        self.assertIsNotNone(final.handle)
        for label in ("P_total", "P_active", "P_damage", "net_doping"):
            np.testing.assert_array_equal(
                final.fields[label].data,
                boundary.fields[label].data,
                err_msg=label,
            )

    def test_conversion_happens_once_per_sequence(self) -> None:
        backend = _CountingCellSetBackend(ps)
        FlowExecutor(
            self.registry,
            _provider(
                _NoCheckpointBackend(
                    ps,
                    id="viennaps",
                    version=str(ps.__version__),
                    material_registry=ViennaPSMaterialRegistry(ps),
                ),
                backend,
            ),
            StateStore(self.work_dir / "v2"),
        ).run(_state(_plane()), _flow_steps(self.registry, self.project, self.raw_steps))
        self.assertEqual(backend.ensures, 1)
        self.assertEqual(backend.generates, 1)
        # One snapshot for the initial CellSetState, one at the boundary.
        self.assertEqual(backend.snapshots, 2)

    def test_mismatched_cell_set_config_inside_a_sequence_is_refused(self) -> None:
        broken = dict(self.raw_steps[1])
        broken["cellSet"] = {**broken["cellSet"], "height": 0.3}
        raw = [self.raw_steps[0], broken]
        with self.assertRaises(Exception) as caught:
            FlowExecutor(
                self.registry,
                _provider(
                    _NoCheckpointBackend(
                        ps,
                        id="viennaps",
                        version=str(ps.__version__),
                        material_registry=ViennaPSMaterialRegistry(ps),
                    )
                ),
                StateStore(self.work_dir / "v2-mismatch"),
            ).run(_state(_plane()), _flow_steps(self.registry, self.project, raw))
        self.assertIn("different config", str(caught.exception))

    def test_geometry_change_between_sequences_fails_closed(self) -> None:
        replacement = _plane(height=0.04)
        self.registry.register_process(
            ProcessSpec(
                id="moveGeometry",
                version="1.0.0",
                label="test geometry move",
                params_model=_MoveParams,
                dimensions=(2,),
                operations=("test",),
            )
        )
        from process_models.v2.contract import VariantRegistration

        self.registry.register_variant(
            VariantRegistration(process_id="moveGeometry", variant=_MoveVariant(replacement))
        )
        move = {
            "id": "move_step_0001",
            "name": "move",
            "enabled": True,
            "model": "moveGeometry",
        }
        first = _flow_steps(self.registry, self.project, [self.raw_steps[0]])[0]
        last = _flow_steps(self.registry, self.project, [self.raw_steps[-1]])[0]
        move_step = FlowStep(
            step_id=move["id"],
            process_id="moveGeometry",
            variant_id="native",
            params=_MoveParams(),
            context=ProcessContext(
                dimension=2,
                grid_delta=GRID_DELTA,
                project=self.project,
                step=move,
            ),
        )
        with self.assertRaises(Exception) as caught:
            FlowExecutor(
                self.registry,
                _provider(
                    _NoCheckpointBackend(
                        ps,
                        id="viennaps",
                        version=str(ps.__version__),
                        material_registry=ViennaPSMaterialRegistry(ps),
                    )
                ),
                StateStore(self.work_dir / "v2-stale"),
            ).run(_state(_plane()), [first, move_step, last])
        self.assertIn("different geometry", str(caught.exception))

    def test_three_dimensional_sequence_matches_the_native_worker_reference(self) -> None:
        project = _project3d()
        reference = _v1_reference(
            project,
            self.raw_steps,
            self.work_dir / "reference-3d",
            domain=_plane3d(),
        )
        run = FlowExecutor(
            self.registry,
            _provider(
                _NoCheckpointBackend(
                    ps,
                    id="viennaps",
                    version=str(ps.__version__),
                    material_registry=ViennaPSMaterialRegistry(ps),
                )
            ),
            StateStore(self.work_dir / "v2-3d"),
        ).run(
            _state(_plane3d(), dimension=3, grid_delta=0.05),
            _flow_steps(self.registry, project, self.raw_steps),
        )
        final = run.state.fields
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final.grid.dimension, 3)
        for label in ("P_total", "P_damage", "P_damage_last", "P_active", "net_doping"):
            np.testing.assert_array_equal(
                final.fields[label].data.reshape(-1, order="F"),
                np.asarray(reference.getScalarData(label), dtype=np.float64),
                err_msg=label,
            )

    def test_canonical_params_control_the_cell_set_lattice(self) -> None:
        canonical = dict(self.raw_steps[0])
        canonical["cellSet"] = {**canonical["cellSet"], "height": 0.2, "substrateDepth": 0.1}
        raw_step = dict(self.raw_steps[0])
        raw_step["cellSet"] = {**raw_step["cellSet"], "height": 0.3, "substrateDepth": 0.2}
        spec, params = self.registry.resolve_step(self.project, canonical)
        outcome = (
            FlowExecutor(
                self.registry,
                _provider(
                    _NoCheckpointBackend(
                        ps,
                        id="viennaps",
                        version=str(ps.__version__),
                        material_registry=ViennaPSMaterialRegistry(ps),
                    )
                ),
                StateStore(self.work_dir / "canonical"),
            )
            .run(
                _state(_plane()),
                [
                    FlowStep(
                        step_id=raw_step["id"],
                        process_id=spec.id,
                        variant_id="native",
                        params=params,
                        context=ProcessContext(
                            dimension=2,
                            grid_delta=GRID_DELTA,
                            project=self.project,
                            step=raw_step,
                        ),
                    )
                ],
            )
            .outcomes[-1]
        )
        fields = outcome.state.fields
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields.provenance["height"], 0.2)
        self.assertEqual(fields.provenance["substrateDepth"], 0.1)

    def test_reloaded_geometry_step_keeps_field_invalidation(self) -> None:
        geometry_step = {
            "id": "planarize_step_01",
            "name": "cmp",
            "enabled": True,
            "model": "planarize",
            "cutoffHeight": -0.05,
        }
        first_two = self.raw_steps[:1] + [geometry_step]
        store = StateStore(self.work_dir / "invalidation")
        run = FlowExecutor(
            self.registry,
            _provider(_normal_backend()),
            store,
        ).run(_state(_plane()), _flow_steps(self.registry, self.project, first_two))
        loaded = store.load_state(
            geometry_step["id"],
            geometry_loader=lambda _ref: run.state.geometry.handle,
        )
        self.assertIsNotNone(loaded.fields)
        assert loaded.fields is not None
        self.assertIsNone(loaded.fields.handle)
        self.assertTrue(loaded.fields.provenance.get("arraysOmitted"))
        with self.assertRaises(Exception) as caught:
            FlowExecutor(
                self.registry,
                _provider(_normal_backend()),
                StateStore(self.work_dir / "invalidation-next"),
            ).run(loaded, _flow_steps(self.registry, self.project, [self.raw_steps[-1]]))
        self.assertIn("different geometry", str(caught.exception))

    def test_later_field_cannot_change_height_while_omitting_depth(self) -> None:
        second = dict(self.raw_steps[1])
        second["cellSet"] = {key: value for key, value in second["cellSet"].items() if key != "substrateDepth"}
        second["cellSet"]["height"] = 0.3
        with self.assertRaises(Exception) as caught:
            FlowExecutor(
                self.registry,
                _provider(
                    _NoCheckpointBackend(
                        ps,
                        id="viennaps",
                        version=str(ps.__version__),
                        material_registry=ViennaPSMaterialRegistry(ps),
                    )
                ),
                StateStore(self.work_dir / "height-conflict"),
            ).run(
                _state(_plane()),
                _flow_steps(self.registry, self.project, [self.raw_steps[0], second]),
            )
        self.assertIn("different config", str(caught.exception))

    def test_a_failed_field_step_aborts_the_whole_run(self) -> None:
        bad = dict(self.raw_steps[-1])
        bad["id"] = "analysis_step_bad"
        bad["netDoping"] = {
            "donorLabels": ["does_not_exist"],
            "acceptorLabels": [],
            "outputLabel": "net_doping",
        }
        after = dict(self.raw_steps[-1])
        after["id"] = "analysis_step_after"
        raw = [*self.raw_steps[:-1], bad, after]

        with self.assertRaises(RuntimeError):
            FlowExecutor(
                self.registry,
                _provider(
                    _NoCheckpointBackend(
                        ps,
                        id="viennaps",
                        version=str(ps.__version__),
                        material_registry=ViennaPSMaterialRegistry(ps),
                    )
                ),
                StateStore(self.work_dir / "v2-failure"),
            ).run(_state(_plane()), _flow_steps(self.registry, self.project, raw))

        # A later step must not have run, and no rollback state was written for
        # the failed step.
        self.assertFalse((self.work_dir / "v2-failure" / "steps" / "analysis_step_after").exists())
        self.assertFalse((self.work_dir / "v2-failure" / "steps" / "analysis_step_bad" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
