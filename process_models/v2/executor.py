"""General multi-step flow executor for v2 process models.

The executor owns the chain

    S_i -> resolve -> checkpoint (mutates_input) -> prepare -> execute
        -> commit -> S_{i+1} -> store

It is a thin, deterministic layer above :func:`resolve_execution` and
:func:`execute_plan`: every step is resolved against the *current* state, its
result is persisted, and the committed state is threaded into the next step.

Checkpointing
-------------
Variants that declare ``VariantCapabilities.mutates_input`` may change the
native geometry handle in place. Before such a step runs, the executor asks the
engine's :class:`~process_models.v2.state_backend.StateBackend` to checkpoint
the state, then re-resolves the request against the copy, so
``plan.request.state`` and the plan's execution both point at the copy and the
previous step's state is left untouched. A backend that cannot checkpoint is a
hard :class:`ExecutionResolutionError`, never a "mutates_input is probably
fine" fallback.

``execution_kind="field"`` steps are the deliberate exception: they mutate an
auxiliary native cell set, not the canonical level set. They do not
checkpoint, their domain/cell-set ownership is transferred directly to the
next step, and a failure aborts the run instead of restoring an earlier state.

Persistence
-----------
For every ordinary step the executor writes ``state.json``/``fields.npz``,
``outputs.json``, and ``plan.json`` through :class:`StateStore`, plus the
geometry reference returned by the engine's state backend (the step's
``geometry_ref`` is that file name, otherwise ``None``). A contiguous
``execution_kind="field"`` sequence is different: intermediate steps write
``state.json`` with ``fieldsOmitted`` (no dense arrays) and no geometry copy;
only the last step of the sequence snapshots the cell set and writes
``fields.npz`` once. After the last step one ``flow.json`` records the run::

    {
      "schemaVersion": 1,
      "runDir": "<absolute run directory>",
      "steps": [<plan identity>, ...],       # in execution order
      "versions": {"<engine id>": "<engine version>"}
    }

Per-step process/variant versions already live in the plan identity, so the
flow-level ``versions`` map only summarises which engine builds the run used.

Version pins
------------
A :class:`FlowStep` may pin ``expected_process_version`` and
``expected_variant_version``. Both are forwarded verbatim into the
:class:`ExecutionRequest`, so a pin that is not registered fails admission
with the registry's ``KeyError`` instead of silently running the newest
version.

Engine leases
-------------
Every :func:`resolve_execution` acquires an engine lease. A ``mutates_input``
step resolves twice (once before checkpointing, once against the copy), so the
first, abandoned plan's lease is released before the second resolution, and
the final lease is released in a ``finally`` after the step has executed and
been persisted. Callers therefore never have to close the leases behind a
returned :class:`ExecutionOutcome`; its plan identity stays readable because
release only drops the acquisition, not the dataclass fields.

Costs
-----
A non-field ``mutates_input`` step copies the native geometry handle before executing.
From the second step on the incoming state is already executor-private (the
previous step committed it), so that is a redundant second copy. It is kept as
an accepted validation cost for now: re-resolution must validate the exact
state the variant will execute on, and reusing an earlier handle would let a
wrong copy/alias corrupt an upstream state instead of failing admission. Do
not "optimise" this away without covering that failure mode.

This module does not snapshot, preview, call an API, or acquire external
engines; it is the runtime foundation only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel

from .adapters import StateAdapter
from .cellset import CellSetBackendError
from .contract import ProcessContext
from .engine import ComputePolicy, EngineProvider
from .registry import ProcessRegistry
from .resolver import (
    ExecutionOutcome,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionResolutionError,
    execute_plan,
    resolve_execution,
)
from .state import ProcessState, StepResult
from .state_backend import StateBackendError
from .storage import StateStore

__all__ = ["FlowExecutor", "FlowRun", "FlowSession", "FlowStep"]

FLOW_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FlowStep:
    """One step in a flow: a process variant plus its parameters and context.

    ``expected_process_version`` and ``expected_variant_version`` are optional
    strict pins. When set, resolution must return exactly that registered
    version; a pin that is not registered aborts the flow before anything is
    written to disk.
    """

    step_id: str
    process_id: str
    variant_id: str
    params: BaseModel
    context: ProcessContext
    expected_process_version: str | None = None
    expected_variant_version: str | None = None
    compute: ComputePolicy | None = None


@dataclass(frozen=True)
class FlowRun:
    """The result of :meth:`FlowExecutor.run`.

    ``outcomes`` follow the input step order; ``state`` is the state committed
    by the last step (or the initial state for an empty flow).
    """

    run_dir: Path
    outcomes: tuple[ExecutionOutcome, ...]
    state: ProcessState


class FlowExecutor:
    """Resolve, execute, checkpoint, and persist a sequence of v2 steps."""

    def __init__(
        self,
        registry: ProcessRegistry,
        provider: EngineProvider,
        store: StateStore,
        adapter: StateAdapter | None = None,
    ) -> None:
        self._registry = registry
        self._provider = provider
        self._store = store
        self._adapter = adapter

    @property
    def store(self) -> StateStore:
        """Return the state store this executor writes to."""
        return self._store

    def run(
        self,
        initial_state: ProcessState,
        steps: Sequence[FlowStep],
        *,
        field_sequence_end: bool | None = None,
    ) -> FlowRun:
        """Execute ``steps`` from ``initial_state`` and persist every result.

        Duplicate or empty step ids are rejected up front, before anything is
        written to disk. ``field_sequence_end`` is for the worker bridge's
        one-step calls: it states whether this field step closes the sequence.
        Multi-step callers let the executor derive boundaries from adjacent
        execution kinds.
        """
        ordered = tuple(steps)
        _check_steps(ordered)
        kinds = self._execution_kinds(ordered)

        state = initial_state
        outcomes: list[ExecutionOutcome] = []
        identities: list[dict[str, str]] = []
        versions: dict[str, str] = {}

        for index, step in enumerate(ordered):
            plan = self._resolve(step, state)
            is_field = plan.process.execution_kind == "field"
            sequence_end = is_field and (index + 1 == len(ordered) or kinds[index + 1] != "field")
            if is_field and field_sequence_end is not None and len(ordered) == 1:
                sequence_end = bool(field_sequence_end)
            try:
                outcome = execute_plan(plan)
                if is_field:
                    if sequence_end:
                        outcome = self._snapshot_field_state(plan, step, outcome)
                        geometry_ref = self._write_geometry(step, plan, outcome.state)
                        self._store.save_state(step.step_id, outcome.state, geometry_ref=geometry_ref)
                    else:
                        # Intermediate field steps share one native handle and
                        # one in-memory state. Their arrays are not a replay
                        # boundary, so do not duplicate the dense fields or
                        # geometry on disk.
                        self._store.save_state(step.step_id, outcome.state, persist_fields=False)
                else:
                    geometry_ref = self._write_geometry(step, plan, outcome.state)
                    # A non-field step is not a cell-set boundary. If a
                    # previous field sequence left a v2 snapshot, keep the
                    # contract metadata but do not write the now-stale dense
                    # arrays a second time; the next field step fails closed on
                    # the geometry digest instead.
                    field_snapshot = (
                        outcome.state.fields is not None
                        and outcome.state.fields.provenance.get("sourceGeometryDigest") is not None
                    )
                    persist_fields = not field_snapshot
                    self._store.save_state(
                        step.step_id,
                        outcome.state,
                        geometry_ref=geometry_ref,
                        persist_fields=persist_fields,
                    )
                self._store.save_outputs(step.step_id, outcome.outputs)
                identities.append(self._store.save_plan(step.step_id, plan))
                versions.setdefault(plan.engine.engine_id, plan.engine_version)
            finally:
                # Release even when execution or persistence raises: the flow
                # is over for this plan either way.
                plan.engine.release()

            outcomes.append(outcome)
            state = outcome.state

        self._store.save_flow(
            {
                "schemaVersion": FLOW_SCHEMA_VERSION,
                "runDir": str(self._store.root),
                "steps": identities,
                "versions": versions,
            }
        )
        return FlowRun(run_dir=self._store.root, outcomes=tuple(outcomes), state=state)

    def _execution_kinds(self, steps: tuple[FlowStep, ...]) -> tuple[str, ...]:
        """Return each step's static process execution kind.

        This runs before the first write so an unknown or pinned-away process
        cannot leave a partial run directory behind, and it lets the loop know
        where a field sequence ends.
        """
        kinds: list[str] = []
        for step in steps:
            resolved = self._registry.get_variant(
                step.process_id,
                step.variant_id,
                expected_process_version=step.expected_process_version,
                expected_variant_version=step.expected_variant_version,
            )
            kinds.append(resolved.process.execution_kind)
        return tuple(kinds)

    def _resolve(self, step: FlowStep, state: ProcessState) -> ExecutionPlan:
        """Resolve ``step`` against ``state``, checkpointing native geometry first.

        A ``mutates_input`` step resolves twice: once against the caller's
        state (admission), then against a copied handle (execution). The first
        plan is abandoned, so its engine lease is released before the second
        resolution acquires another one.

        ``execution_kind="field"`` steps deliberately skip checkpointing: they
        mutate the auxiliary native cell set, not the canonical level set, and
        their native handle is transferred to the next step in the sequence.
        """
        plan = resolve_execution(self._request(step, state), self._registry, self._provider, adapter=self._adapter)
        if plan.process.execution_kind == "field":
            return plan
        if not plan.variant.capabilities.mutates_input:
            return plan

        try:
            checkpointed = self._checkpoint(plan, state)
        except BaseException:
            plan.engine.release()
            raise
        if checkpointed is state:
            return plan

        plan.engine.release()
        return resolve_execution(
            self._request(step, checkpointed),
            self._registry,
            self._provider,
            adapter=self._adapter,
        )

    @staticmethod
    def _request(step: FlowStep, state: ProcessState) -> ExecutionRequest:
        return ExecutionRequest(
            process_id=step.process_id,
            variant_id=step.variant_id,
            params=step.params,
            context=step.context,
            state=state,
            expected_process_version=step.expected_process_version,
            expected_variant_version=step.expected_variant_version,
            compute=step.compute,
        )

    @staticmethod
    def _checkpoint(plan: ExecutionPlan, state: ProcessState) -> ProcessState:
        """Return ``state`` with a copied native geometry handle.

        The copy comes from the engine's :class:`StateBackend`. Derived voxels
        belong to the old handle, so they are dropped just like a committed
        geometry delta does. A state without geometry has nothing native to
        checkpoint and is returned unchanged.
        """
        if state.geometry is None:
            return state
        backend = plan.engine.state_backend
        try:
            return backend.checkpoint(state)
        except StateBackendError as error:
            raise ExecutionResolutionError(
                f"variant {plan.process.id!r}/{plan.variant.variant_id!r} declares "
                f"mutates_input=True but state backend {backend.id!r} cannot checkpoint "
                f"the native geometry: {error}"
            ) from error

    def _snapshot_field_state(
        self,
        plan: ExecutionPlan,
        step: FlowStep,
        outcome: ExecutionOutcome,
    ) -> ExecutionOutcome:
        """Snapshot the native cell set once at a field-sequence boundary."""
        fields = outcome.state.fields
        if fields is None:
            raise ExecutionResolutionError(f"field step {step.step_id!r} completed without cell-set state")
        backend = getattr(plan.engine, "cellset_backend", None)
        if backend is None:
            raise ExecutionResolutionError(
                f"field step {step.step_id!r} has no cell-set backend on engine {plan.engine.engine_id!r}"
            )
        try:
            snapshot = backend.snapshot(
                fields.handle,
                step.context,
                base=fields,
                geometry=outcome.state.geometry,
                source_step_id=step.step_id,
            )
        except CellSetBackendError as error:
            raise ExecutionResolutionError(
                f"could not snapshot the cell set at the end of the field sequence for step {step.step_id!r}: {error}"
            ) from error
        result = outcome.result
        delta = result.delta
        if delta.fields is not None:
            delta = replace(delta, fields=snapshot)
        return ExecutionOutcome(
            plan=outcome.plan,
            state=replace(outcome.state, fields=snapshot),
            result=StepResult(
                delta=delta,
                outputs=result.outputs,
                metadata=result.metadata,
                warnings=result.warnings,
            ),
        )

    def _write_geometry(self, step: FlowStep, plan: ExecutionPlan, state: ProcessState) -> str | None:
        """Persist the step's native geometry through the state backend.

        Returns the file name relative to the step directory, or ``None`` when
        the backend does not persist geometry or the step has no geometry.
        """
        if state.geometry is None:
            return None
        try:
            reference = plan.engine.state_backend.write(state, self._store.step_dir(step.step_id))
        except StateBackendError as error:
            raise ExecutionResolutionError(
                f"state backend {plan.engine.state_backend.id!r} could not write geometry "
                f"for step {step.step_id!r}: {error}"
            ) from error
        return None if reference is None else reference.path


@dataclass(frozen=True)
class FlowSession:
    """Reusable per-run execution context for one-step-at-a-time callers.

    ``FlowExecutor`` already owns the execution semantics.  The missing piece
    for a worker that must snapshot after every step is a way to reuse the
    expensive, run-scoped objects - registry, engine provider, and adapter -
    without batching all steps into one ``FlowExecutor.run()`` call (which
    would hide the per-step snapshot / GPU teardown boundaries).

    ``store_factory`` receives the step id and returns that step's
    :class:`StateStore`.  A per-step store preserves the existing replay
    layout: every step keeps its own ``state.json`` / ``outputs.json`` /
    ``plan.json`` / ``geometry.vpsd``.  The session only removes the repeated
    registry/provider/adapter setup; it does not remove per-step persistence.
    """

    registry: ProcessRegistry
    provider: EngineProvider
    store_factory: Callable[[str], StateStore]
    adapter: StateAdapter | None = None

    def run_step(
        self,
        initial_state: ProcessState,
        step: FlowStep,
        *,
        field_sequence_end: bool = True,
    ) -> ExecutionOutcome:
        """Resolve, execute, and persist exactly one step.

        The call is intentionally one step at a time.  Callers that need a
        snapshot, an event, or a GPU teardown after every step keep their
        existing loop; only the run-scoped setup is shared.
        """
        store = self.store_factory(step.step_id)
        executor = FlowExecutor(self.registry, self.provider, store, self.adapter)
        run = executor.run(initial_state, [step], field_sequence_end=field_sequence_end)
        return run.outcomes[-1]


def _check_steps(steps: tuple[FlowStep, ...]) -> None:
    """Reject non-step entries and duplicate/blank step ids."""
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, FlowStep):
            raise TypeError(f"flow steps must be FlowStep instances, got {type(step).__name__}")
        if not isinstance(step.step_id, str) or not step.step_id.strip():
            raise ValueError(f"step_id must be a non-empty string, got {step.step_id!r}")
        if step.step_id in seen:
            raise ValueError(f"duplicate step_id {step.step_id!r} in one flow")
        seen.add(step.step_id)
