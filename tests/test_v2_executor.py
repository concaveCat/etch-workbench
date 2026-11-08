"""Tests for the v2 multi-step flow executor.

Every test builds a small real 2D ViennaPS plane, loads the discovered
isotropic plugin through :func:`load_registry`, and drives it through a
:class:`FlowExecutor` with a :class:`StateStore` rooted in a temporary
directory. No server, worker, or workbench data directory is involved.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import viennaps as ps
from pydantic import BaseModel

from plugins.isotropic import IsotropicParams
from process_models.plugins import load_registry
from process_models.v2.contract import (
    ProcessContext,
    ProcessSpec,
    VariantCapabilities,
    VariantRegistration,
)
from process_models.v2.engine import (
    EngineCapabilities,
    EngineRequirement,
    ExecutionProfile,
    StaticEngine,
    StaticEngineProvider,
)
from process_models.v2.executor import FlowExecutor, FlowRun, FlowSession, FlowStep
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionResolutionError
from process_models.v2.state import (
    CellSetState,
    Field,
    GridSpec,
    Layer,
    LayerState,
    LevelSetState,
    MaterialState,
    OutputSet,
    ProcessState,
    StateDelta,
    StepResult,
)
from process_models.v2.state_backend import HandleStateBackend
from process_models.v2.storage import StateStore

GRID_DELTA = 0.2
X_EXTENT = 4.0
FINGERPRINT = "test-flow-executor-fingerprint"
VIENNAPS_VERSION = str(ps.version)
SI = int(ps.Material.Si)
SIO2 = int(ps.Material.SiO2)


def _plane() -> Any:
    """Return a fresh 2D reflective plane of Si at height 0."""
    ps.setDimension(2)
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def _material_order(domain: Any) -> tuple[int, ...]:
    mapping = domain.getMaterialMap()
    return tuple(int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size()))


def _top(domain: Any) -> float:
    """Return the highest surface-mesh y coordinate of a 2D domain."""
    mesh = domain.getSurfaceMesh(addInterfaces=True, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in node] for node in mesh.getNodes()]
    if not points:
        raise AssertionError("domain has no surface nodes")
    return float(np.max(np.asarray(points, dtype=float)[:, 1]))


def _read_geometry(path: Path) -> Any:
    """Native replay loader: a fresh domain plus ViennaPS ``Reader``."""
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=X_EXTENT,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    ps.Reader(domain, str(path)).apply()
    return domain


def _layers() -> LayerState:
    return LayerState(
        (
            Layer(
                layer_id="substrate",
                material_id=SI,
                name="Si",
                source_step="initial",
                thickness=0.1,
                metadata={"role": "substrate"},
            ),
        )
    )


def _fields() -> CellSetState:
    grid = GridSpec(dimension=2, origin=(-1.0, -1.0), spacing=0.1, shape=(4, 5))
    values = np.arange(20, dtype=np.float64).reshape(4, 5) / 20.0
    return CellSetState(
        grid=grid,
        material_ids=np.full((4, 5), SI, dtype=np.int32),
        fields={
            "P_total": Field(
                name="P_total",
                unit="1/cm3",
                association="cell",
                dtype="float64",
                shape=(4, 5),
                data=values,
            )
        },
        provenance={"producer": "test-v2-executor"},
    )


def _state(domain: Any) -> ProcessState:
    order = _material_order(domain)
    return ProcessState(
        geometry=LevelSetState(
            handle=domain,
            dimension=2,
            grid_delta=GRID_DELTA,
        ),
        materials=MaterialState(order=order, names={SI: "Si"}),
        layers=_layers(),
        fields=_fields(),
    )


def _context() -> ProcessContext:
    return ProcessContext(
        dimension=2,
        grid_delta=GRID_DELTA,
        project={
            "schemaVersion": 3,
            "dimension": 2,
            "geometry": {"kind": "plane", "substrateMaterial": "Si"},
            "solver": {"timeStepRatio": 0.25},
        },
    )


def _provider(handle: Any = ps) -> StaticEngineProvider:
    capabilities = EngineCapabilities(
        engine_id="viennaps",
        version=VIENNAPS_VERSION,
        kind="native",
        backends=("cpu",),
    )
    return StaticEngineProvider([StaticEngine(capabilities=capabilities, handle=handle, fingerprint=FINGERPRINT)])


def _deposit_step() -> FlowStep:
    return FlowStep(
        step_id="deposit",
        process_id="isotropic",
        variant_id="native",
        params=IsotropicParams(
            operation="deposit",
            rateMagnitude=0.3,
            duration=1.0,
            depositMaterial="SiO2",
        ),
        context=_context(),
    )


def _etch_step() -> FlowStep:
    return FlowStep(
        step_id="etch",
        process_id="isotropic",
        variant_id="native",
        params=IsotropicParams(operation="etch", rateMagnitude=0.2, duration=1.0),
        context=_context(),
    )


class _HandleProxy:
    """Module proxy that hides one ViennaPS attribute (``None`` on getattr)."""

    def __init__(self, module: Any, hidden: str) -> None:
        self._module = module
        self._hidden = hidden

    def __getattr__(self, name: str) -> Any:
        if name == self._hidden:
            raise AttributeError(f"{self._hidden!r} is hidden by the test proxy")
        return getattr(self._module, name)


# -- fake engine/variant pair for lease-release accounting ------------------


class _FakeParams(BaseModel):
    """Minimal parameter model for the fake mutating process."""

    label: str = "fake"


class _FakeDomain:
    """Native-handle stub whose ``Domain`` copy constructor records copies."""

    def __init__(self, copies: list["_FakeDomain"]) -> None:
        self._copies = copies

    def Domain(self, other: "_FakeDomain") -> "_FakeDomain":  # noqa: N802 - ViennaPS spelling
        self._copies.append(other)
        return _FakeDomain(self._copies)


class _FakeLease:
    """Lease stub that counts ``release`` calls."""

    def __init__(self, handle: Any, events: list[str], name: str) -> None:
        self.engine_id = "fake"
        self.engine_version = "1.0.0"
        self.kind = "native"
        self.profile = ExecutionProfile(backend="cpu", kernel="CPU_DISK")
        self.backend = self.profile.backend
        self.state_backend = HandleStateBackend(handle)
        self.fingerprint = name
        self.handle = handle
        self.released = 0
        self._events = events

    def release(self) -> None:
        self.released += 1
        self._events.append(f"release:{self.fingerprint}")

    def __enter__(self) -> "_FakeLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class _FakeProvider:
    """Provider that hands out :class:`_FakeLease` objects and records order."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self.events: list[str] = []
        self.leases: list[_FakeLease] = []

    def engines(self) -> list[EngineCapabilities]:
        return [
            EngineCapabilities(engine_id="fake", version="1.0.0", kind="native", backends=("cpu",)),
        ]

    def acquire(self, requirement: EngineRequirement, policy: Any = None) -> _FakeLease:
        del requirement, policy
        name = f"lease{len(self.leases)}"
        lease = _FakeLease(self._handle, self.events, name)
        self.leases.append(lease)
        self.events.append(f"acquire:{name}")
        return lease


class _FakeMutatingVariant:
    """In-place geometry step: returns the executed handle as its delta."""

    variant_id = "fake"
    version = "1.0.0"
    capabilities = VariantCapabilities(
        dimensions=(2,),
        reads=frozenset({"geometry"}),
        writes=frozenset({"geometry"}),
        mutates_input=True,
    )
    engine = EngineRequirement(kind="native", engine_id="fake", min_version="1.0.0")
    implementation_hash = None

    def __init__(self) -> None:
        self.executed_handles: list[Any] = []

    def validate(self, ctx: ProcessContext, params: BaseModel) -> list[Any]:
        return []

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        self.executed_handles.append(state.geometry.handle)
        return StepResult(delta=StateDelta(geometry=state.geometry))


class _ExplodingVariant(_FakeMutatingVariant):
    """Mutating variant whose execution always fails."""

    def execute(self, state: ProcessState, params: BaseModel, ctx: ProcessContext, engine: Any) -> StepResult:
        raise RuntimeError("boom")


def _fake_registry(variant: Any) -> ProcessRegistry:
    registry = ProcessRegistry()
    registry.register_process(
        ProcessSpec(
            id="fake",
            version="1.0.0",
            label="Fake",
            params_model=_FakeParams,
            dimensions=(2,),
            operations=("fake",),
        )
    )
    registry.register_variant(VariantRegistration(process_id="fake", variant=variant))
    return registry


def _fake_state(handle: _FakeDomain) -> ProcessState:
    return ProcessState(
        geometry=LevelSetState(handle=handle, dimension=2, grid_delta=GRID_DELTA),
        materials=MaterialState(()),
    )


def _fake_step() -> FlowStep:
    return FlowStep(
        step_id="fake",
        process_id="fake",
        variant_id="fake",
        params=_FakeParams(),
        context=ProcessContext(dimension=2, grid_delta=GRID_DELTA),
    )


class FlowExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.setDimension(2)
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "run"
        self.store = StateStore(self.root)
        self.registry = load_registry()
        self.provider = _provider()
        self.executor = FlowExecutor(self.registry, self.provider, self.store)

    def _run_two_step_flow(self) -> tuple[Any, ProcessState, FlowRun]:
        domain = _plane()
        state = _state(domain)
        run = self.executor.run(state, [_deposit_step(), _etch_step()])
        return domain, state, run

    # -- execution and persistence ----------------------------------------

    def test_two_step_flow_executes_threads_and_persists(self) -> None:
        domain = _plane()
        initial_state = _state(domain)
        initial_top = _top(domain)

        run = self.executor.run(initial_state, [_deposit_step(), _etch_step()])

        self.assertIsInstance(run, FlowRun)
        self.assertEqual(run.run_dir, self.root)
        self.assertEqual(len(run.outcomes), 2)
        self.assertIs(run.state, run.outcomes[-1].state)

        # The initial native geometry was checkpointed, never executed in place.
        self.assertIs(initial_state.geometry.handle, domain)
        self.assertAlmostEqual(_top(domain), initial_top, delta=1e-12)
        self.assertEqual(domain.getNumberOfLevelSets(), 1)

        first, second = run.outcomes
        self.assertIsNot(first.state.geometry.handle, domain)
        self.assertIsNot(second.state.geometry.handle, domain)
        self.assertIsNot(first.plan.request.state, initial_state)
        self.assertIsNot(second.plan.request.state, first.state)

        # deposit +0.3 then uniform etch -0.2 leaves the surface at +0.1.
        self.assertAlmostEqual(_top(run.state.geometry.handle) - initial_top, 0.1, delta=1e-6)
        self.assertEqual(run.state.materials.order, (SI, SIO2))
        self.assertEqual(run.state.materials.names, {SI: "Si"})

        # The deposit's materials delta is threaded into the next step.
        self.assertEqual(first.delta.materials.order, (SI, SIO2))
        self.assertIsNot(second.plan.request.state.materials, initial_state.materials)
        self.assertEqual(second.plan.request.state.materials.order, (SI, SIO2))
        self.assertEqual(second.result.delta.materials.order, (SI, SIO2))

        # Layers and fields survive across steps even though isotropic only writes geometry.
        self.assertIsNotNone(run.state.layers)
        self.assertIsNotNone(run.state.fields)

        self._assert_step_files("deposit")
        self._assert_step_files("etch")

        flow_path = self.root / "flow.json"
        self.assertTrue(flow_path.is_file())
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
        self.assertEqual(flow["schemaVersion"], 1)
        self.assertEqual(flow["runDir"], str(self.root))
        self.assertEqual(flow["steps"], [outcome.plan.identity_json() for outcome in run.outcomes])
        self.assertEqual(flow["versions"], {"viennaps": VIENNAPS_VERSION})

    def _assert_step_files(self, step_id: str) -> None:
        directory = self.store.step_dir(step_id)
        for name in ("state.json", "outputs.json", "plan.json", "geometry.vpsd", "fields.npz"):
            with self.subTest(step=step_id, file=name):
                path = directory / name
                self.assertTrue(path.is_file(), f"{path} is missing")
                self.assertGreater(path.stat().st_size, 0, f"{path} is empty")

        payload = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["stepId"], step_id)
        self.assertEqual(payload["geometry"]["ref"], "geometry.vpsd")
        self.assertEqual(payload["geometry"]["dimension"], 2)
        self.assertEqual(payload["geometry"]["gridDelta"], GRID_DELTA)

        plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["processId"], "isotropic")
        self.assertEqual(plan["variantId"], "native")
        self.assertEqual(plan["engineId"], "viennaps")
        self.assertEqual(plan["engineVersion"], VIENNAPS_VERSION)
        self.assertEqual(plan["engineFingerprint"], FINGERPRINT)

    def test_state_json_metadata_and_outputs_round_trip(self) -> None:
        _domain, _state_input, run = self._run_two_step_flow()

        payload = json.loads((self.store.step_dir("etch") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["materials"]["order"], [SI, SIO2])
        self.assertEqual(payload["materials"]["names"], {str(SI): "Si"})
        # Material order lives only in the canonical materials block.
        self.assertNotIn("materialOrder", payload["geometry"])
        self.assertEqual(
            payload["layers"],
            {
                "layers": [
                    {
                        "layerId": "substrate",
                        "materialId": SI,
                        "name": "Si",
                        "sourceStep": "initial",
                        "thickness": 0.1,
                        "metadata": {"role": "substrate"},
                    }
                ]
            },
        )
        self.assertEqual(
            payload["fields"]["grid"],
            {"dimension": 2, "origin": [-1.0, -1.0], "spacing": 0.1, "shape": [4, 5]},
        )
        self.assertEqual(
            payload["fields"]["fields"]["P_total"],
            {"unit": "1/cm3", "association": "cell", "dtype": "float64", "shape": [4, 5], "components": 1},
        )
        self.assertEqual(payload["fields"]["materialIds"], {"dtype": "int32", "shape": [4, 5]})

        self.assertEqual(self.store.load_outputs("deposit"), OutputSet())
        self.assertEqual(self.store.load_outputs("etch"), OutputSet())
        self.assertTrue(run.outcomes[0].outputs == OutputSet())

    def test_load_state_with_geometry_loader_restores_the_saved_state(self) -> None:
        _domain, _state_input, run = self._run_two_step_flow()
        final = run.state

        loaded = self.store.load_state(
            "etch",
            geometry_loader=lambda reference: _read_geometry(self.store.step_dir("etch") / reference),
        )

        self.assertIsNotNone(loaded.geometry)
        self.assertEqual(loaded.geometry.dimension, 2)
        self.assertEqual(loaded.geometry.grid_delta, GRID_DELTA)
        self.assertEqual(loaded.materials.order, final.materials.order)
        self.assertAlmostEqual(_top(loaded.geometry.handle), _top(final.geometry.handle), delta=1e-6)

        self.assertEqual(loaded.materials, final.materials)
        self.assertEqual(loaded.layers, final.layers)
        self.assertIsNotNone(loaded.fields)
        self.assertEqual(loaded.fields.grid, final.fields.grid)
        self.assertEqual(
            set(loaded.fields.fields),
            set(final.fields.fields),
        )
        np.testing.assert_array_equal(
            loaded.fields.fields["P_total"].data,
            final.fields.fields["P_total"].data,
        )
        np.testing.assert_array_equal(loaded.fields.material_ids, final.fields.material_ids)
        self.assertEqual(loaded.fields.provenance, final.fields.provenance)

    def test_second_run_continues_from_the_final_state(self) -> None:
        _domain, _state_input, first_run = self._run_two_step_flow()
        final = first_run.state
        final_top = _top(final.geometry.handle)

        second_root = Path(self._temporary.name) / "second-run"
        second_store = StateStore(second_root)
        second_executor = FlowExecutor(self.registry, self.provider, second_store)
        second_run = second_executor.run(final, [_etch_step()])

        self.assertEqual(second_run.run_dir, second_root)
        self.assertEqual(len(second_run.outcomes), 1)
        self.assertIsNot(second_run.state, final)
        self.assertIsNot(second_run.outcomes[0].plan.request.state, final)

        # The first run's final geometry is checkpointed, not mutated.
        self.assertAlmostEqual(_top(final.geometry.handle), final_top, delta=1e-12)
        self.assertAlmostEqual(_top(second_run.state.geometry.handle), final_top - 0.2, delta=1e-6)
        self.assertEqual(second_run.state.materials.order, (SI, SIO2))

        flow = json.loads((second_root / "flow.json").read_text(encoding="utf-8"))
        self.assertEqual(flow["steps"], [second_run.outcomes[0].plan.identity_json()])
        self.assertEqual(flow["versions"], {"viennaps": VIENNAPS_VERSION})
        self.assertTrue((second_store.step_dir("etch") / "state.json").is_file())

    # -- version pins ------------------------------------------------------

    def test_flow_step_version_pins_are_forwarded_to_the_plan(self) -> None:
        resolved = self.registry.get_variant("isotropic", "native")
        pinned = replace(
            _deposit_step(),
            expected_process_version=resolved.process_version,
            expected_variant_version=resolved.variant_version,
        )

        run = self.executor.run(_state(_plane()), [pinned])

        plan = run.outcomes[0].plan
        self.assertEqual(plan.request.expected_process_version, resolved.process_version)
        self.assertEqual(plan.request.expected_variant_version, resolved.variant_version)
        self.assertEqual(plan.process_version, resolved.process_version)
        self.assertEqual(plan.variant_version, resolved.variant_version)

        stored = json.loads((self.store.step_dir("deposit") / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["processVersion"], resolved.process_version)
        self.assertEqual(stored["variantVersion"], resolved.variant_version)

    def test_flow_step_wrong_version_pin_aborts_before_any_write(self) -> None:
        root = Path(self._temporary.name) / "pinned-miss"
        store = StateStore(root)
        executor = FlowExecutor(self.registry, self.provider, store)

        for pin in ("expected_process_version", "expected_variant_version"):
            with self.subTest(pin=pin):
                step = replace(_deposit_step(), **{pin: "9.9.9"})
                with self.assertRaises(KeyError) as raised:
                    executor.run(_state(_plane()), [step])
                self.assertIn("9.9.9", str(raised.exception))

        self.assertFalse((root / "flow.json").exists())
        self.assertFalse(store.step_dir("deposit").exists())

    # -- engine lease release ---------------------------------------------

    def test_mutates_input_releases_the_first_lease_before_re_resolving(self) -> None:
        copies: list[_FakeDomain] = []
        variant = _FakeMutatingVariant()
        provider = _FakeProvider(_FakeDomain(copies))
        root = Path(self._temporary.name) / "leases"
        store = StateStore(root)
        executor = FlowExecutor(_fake_registry(variant), provider, store)

        run = executor.run(_fake_state(_FakeDomain(copies)), [_fake_step()])

        self.assertEqual(
            provider.events,
            ["acquire:lease0", "release:lease0", "acquire:lease1", "release:lease1"],
        )
        self.assertEqual([lease.released for lease in provider.leases], [1, 1])
        self.assertEqual(len(copies), 1)  # one checkpoint copy, unchanged behaviour
        # A released lease's identity stays readable on the returned outcome.
        self.assertEqual(run.outcomes[0].plan.engine.fingerprint, "lease1")
        self.assertEqual(run.outcomes[0].plan.identity()["engine_id"], "fake")
        self.assertTrue((store.step_dir("fake") / "plan.json").is_file())

    def test_failed_execution_still_releases_the_lease(self) -> None:
        copies: list[_FakeDomain] = []
        provider = _FakeProvider(_FakeDomain(copies))
        root = Path(self._temporary.name) / "failed"
        store = StateStore(root)
        executor = FlowExecutor(_fake_registry(_ExplodingVariant()), provider, store)

        with self.assertRaises(RuntimeError):
            executor.run(_fake_state(_FakeDomain(copies)), [_fake_step()])

        self.assertEqual(
            provider.events,
            ["acquire:lease0", "release:lease0", "acquire:lease1", "release:lease1"],
        )
        self.assertFalse((root / "flow.json").exists())
        self.assertFalse(store.step_dir("fake").exists())

    # -- engines without checkpoint/writer support ------------------------

    def test_engine_without_domain_cannot_checkpoint(self) -> None:
        root = Path(self._temporary.name) / "no-domain"
        store = StateStore(root)
        executor = FlowExecutor(self.registry, _provider(_HandleProxy(ps, "Domain")), store)

        with self.assertRaises(ExecutionResolutionError) as raised:
            executor.run(_state(_plane()), [_deposit_step()])
        message = str(raised.exception)
        self.assertIn("mutates_input", message)
        self.assertIn("Domain", message)
        self.assertFalse((root / "flow.json").exists())
        self.assertFalse(store.step_dir("deposit").exists())

    def test_engine_without_writer_records_no_geometry_ref(self) -> None:
        executor = FlowExecutor(self.registry, _provider(_HandleProxy(ps, "Writer")), self.store)
        run = executor.run(_state(_plane()), [_etch_step()])

        directory = self.store.step_dir("etch")
        self.assertFalse((directory / "geometry.vpsd").exists())
        payload = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        self.assertIsNone(payload["geometry"]["ref"])
        self.assertIsNotNone(run.state.geometry)


class FlowSessionTest(unittest.TestCase):
    """Per-step execution with run-scoped registry/provider reuse."""

    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.setDimension(2)
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "session-run"
        self.registry = load_registry()
        self.provider = _provider()
        self.stores: dict[str, StateStore] = {}

    def _store_factory(self, step_id: str) -> StateStore:
        store = StateStore(self.root / step_id)
        self.stores[step_id] = store
        return store

    def test_run_step_reuses_run_scope_and_keeps_per_step_replay(self) -> None:
        session = FlowSession(self.registry, self.provider, self._store_factory)
        initial = _state(_plane())
        initial_top = _top(initial.geometry.handle)

        first = session.run_step(initial, _deposit_step())
        second = session.run_step(first.state, _etch_step())

        self.assertIs(session.registry, self.registry)
        self.assertIs(session.provider, self.provider)
        # mutates_input is still checkpointed once per step: the plan executes
        # on a private copy, never on the caller's state or the previous plan.
        self.assertIsNot(first.plan.request.state, initial)
        self.assertIsNot(first.plan.request.state.geometry.handle, initial.geometry.handle)
        self.assertIsNot(second.plan.request.state, first.state)
        self.assertAlmostEqual(_top(second.state.geometry.handle) - initial_top, 0.1, delta=1e-6)

        # Each step keeps its own replay artifacts; the session only removes
        # repeated registry/provider setup, not per-step persistence.
        for step_id in ("deposit", "etch"):
            store = self.stores[step_id]
            directory = store.step_dir(step_id)
            for name in ("state.json", "outputs.json", "plan.json", "geometry.vpsd", "fields.npz"):
                with self.subTest(step=step_id, file=name):
                    self.assertTrue((directory / name).is_file(), f"{directory / name} is missing")
            self.assertTrue((self.root / step_id / "flow.json").is_file())
            plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["processId"], "isotropic")
            self.assertEqual(plan["engineFingerprint"], FINGERPRINT)


if __name__ == "__main__":
    unittest.main()
