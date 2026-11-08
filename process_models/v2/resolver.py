"""Execution resolution for v2 process models.

``resolve_execution`` is the admission gate between a saved project step and a
concrete implementation. It performs only cheap, deterministic checks and
acquires an engine lease. It never calls ``variant.execute``: the executor
does that with the returned :class:`ExecutionPlan`, then calls the adapter's
``commit``.

Phase-1 boundaries
------------------
No scheduling, persistence, retries, or GPU arbitration. Engine acquisition is
strict; an unavailable engine is an error and never silently falls back to
another version, backend, or implementation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .adapters import DefaultStateAdapter
from .cellset import CellSetBackendError, CellSetConfig
from .contract import Issue, ProcessContext, ProcessSpec, StateProfile, VariantRegistration
from .engine import ComputePolicy, EngineLease, EngineProvider, EngineUnavailableError
from .materials import MaterialRegistryError, missing_material_names
from .registry import ProcessRegistry
from .state import Artifact, OutputSet, ProcessState, StateDelta, StateKind, StepResult
from .state_backend import StateBackendError

if TYPE_CHECKING:
    from .adapters import StateAdapter

__all__ = [
    "ExecutionOutcome",
    "ExecutionPlan",
    "ExecutionRequest",
    "ExecutionResolutionError",
    "StateContract",
    "execute_plan",
    "resolve_execution",
    "resolve_state_contract",
    "run_step",
]


class ExecutionResolutionError(RuntimeError):
    """Raised when a request cannot be mapped to a runnable execution plan."""


@dataclass(frozen=True)
class ExecutionRequest:
    """Everything needed to admit one step.

    ``params`` must already be an instance of the process's ``params_model``;
    the resolver does not coerce or re-validate Pydantic input.
    """

    process_id: str
    variant_id: str
    params: BaseModel
    context: ProcessContext
    state: ProcessState
    expected_process_version: str | None = None
    expected_variant_version: str | None = None
    compute: ComputePolicy | None = None


@dataclass(frozen=True)
class StateContract:
    """The resolved state agreement for one execution.

    ``identity()`` is deliberately primitive and JSON-safe so it can be hashed
    into the plan identity. The contract records the *selected* backend and the
    resolved conversion/material/persistence policy, not the plugin's raw
    declaration.
    """

    backend_id: str
    backend_version: str
    dimension: int
    grid_delta: float
    conversions: tuple[str, ...] = ()
    required_materials: tuple[str, ...] = ()
    persistence: frozenset[StateKind] = frozenset()

    def identity(self) -> dict[str, object]:
        return {
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "dimension": int(self.dimension),
            "grid_delta": float(self.grid_delta),
            "conversions": list(self.conversions),
            "required_materials": list(self.required_materials),
            "persistence": sorted(self.persistence),
        }

    def digest(self) -> str:
        payload = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionOutcome:
    """The committed state plus the :class:`StepResult` that produced it.

    ``state`` is what the adapter committed from ``result.delta``; outputs,
    metadata, and warnings stay on the result and are read through the
    delegating properties below.
    """

    plan: ExecutionPlan
    state: ProcessState
    result: StepResult

    @property
    def delta(self) -> StateDelta:
        return self.result.delta

    @property
    def metadata(self) -> dict[str, object]:
        return self.result.metadata

    @property
    def warnings(self) -> list[str]:
        return self.result.warnings

    @property
    def outputs(self) -> OutputSet:
        return self.result.outputs

    @property
    def artifacts(self) -> list[Artifact]:
        return list(self.result.outputs.artifacts)


@dataclass(frozen=True)
class ExecutionPlan:
    """A resolved, engine-backed execution without side effects.

    The plan owns the :class:`ExecutionRequest` it was resolved from, so
    :meth:`run` and :func:`execute_plan` never need duplicate parameters.
    """

    request: ExecutionRequest
    process: ProcessSpec
    variant: VariantRegistration
    engine: EngineLease
    state_adapter: StateAdapter
    state_contract: StateContract
    process_version: str
    variant_version: str
    implementation_hash: str
    engine_version: str
    engine_fingerprint: str

    def identity(self) -> dict[str, str]:
        """Return the stable, JSON-safe identity of this plan.

        These keys are the replay/caching contract: two plans with the same
        identity ran the same process version through the same implementation
        on the same engine build, with the same execution profile, state
        backend, state contract, and representation adapter.
        """
        return {
            "process_id": self.process.id,
            "process_version": self.process_version,
            "variant_id": self.variant.variant_id,
            "variant_version": self.variant_version,
            "implementation_hash": self.implementation_hash,
            "engine_id": self.engine.engine_id,
            "engine_version": self.engine_version,
            "engine_fingerprint": self.engine_fingerprint,
            "adapter": self.state_adapter.id,
            "engine_backend": self.engine.profile.backend,
            "engine_kernel": self.engine.profile.kernel or "",
            "engine_device": self.engine.profile.device or "",
            "engine_strategy": self.engine.profile.strategy or "",
            "state_backend_id": self.state_contract.backend_id,
            "state_backend_version": self.state_contract.backend_version,
            "state_contract": self.state_contract.digest(),
        }

    def identity_json(self) -> dict[str, str]:
        """Return the JSON-boundary identity in camelCase.

        ``identity()`` stays the internal snake_case key set; storage and API
        documents use this stable camelCase form.
        """
        identity = self.identity()
        return {
            "processId": identity["process_id"],
            "processVersion": identity["process_version"],
            "variantId": identity["variant_id"],
            "variantVersion": identity["variant_version"],
            "implementationHash": identity["implementation_hash"],
            "engineId": identity["engine_id"],
            "engineVersion": identity["engine_version"],
            "engineFingerprint": identity["engine_fingerprint"],
            "adapter": identity["adapter"],
            "engineBackend": identity["engine_backend"],
            "engineKernel": identity["engine_kernel"],
            "engineDevice": identity["engine_device"],
            "engineStrategy": identity["engine_strategy"],
            "stateBackendId": identity["state_backend_id"],
            "stateBackendVersion": identity["state_backend_version"],
            "stateContract": identity["state_contract"],
        }

    def run(self) -> ExecutionOutcome:
        """Execute this plan, equivalent to ``execute_plan(self)``."""
        return execute_plan(self)


def resolve_execution(
    request: ExecutionRequest,
    registry: ProcessRegistry,
    provider: EngineProvider,
    adapter: StateAdapter | None = None,
) -> ExecutionPlan:
    """Resolve ``request`` to an :class:`ExecutionPlan` or raise.

    Checks, in order: registry identity, parameter type, dimension, required
    state representations, variant validation, and engine availability. A
    failed check raises :class:`ExecutionResolutionError` without retrying a
    different variant or engine.
    """
    resolved = registry.get_variant(
        request.process_id,
        request.variant_id,
        expected_process_version=request.expected_process_version,
        expected_variant_version=request.expected_variant_version,
    )
    process = resolved.process
    variant = resolved.variant
    capabilities = variant.capabilities

    if not isinstance(request.params, process.params_model):
        raise ExecutionResolutionError(
            f"process {process.id!r} version {process.version!r} expects params "
            f"{process.params_model.__name__}, got {type(request.params).__name__}"
        )

    dimension = request.context.dimension
    if dimension not in capabilities.dimensions:
        raise ExecutionResolutionError(
            f"variant {process.id!r}/{variant.variant_id!r} does not support dimension {dimension}; "
            f"supported dimensions are {list(capabilities.dimensions)}"
        )

    _check_geometry_grid(request.state, request.context)
    selected_adapter = adapter or DefaultStateAdapter()
    # A fields read may be satisfied by the engine's cell-set backend, which is
    # only known after acquisition. Defer that one check for the default
    # adapter; a custom adapter must declare its own `provides` up front.
    deferred_fields = adapter is None and "fields" in capabilities.reads
    missing = sorted(
        kind
        for kind in capabilities.reads
        if not (deferred_fields and kind == "fields") and not _state_provides(request.state, kind, selected_adapter)
    )
    if missing:
        raise ExecutionResolutionError(
            f"variant {process.id!r}/{variant.variant_id!r} requires state kinds {missing}, "
            f"but the process state does not provide them"
        )

    issues = list(variant.variant.validate(request.context, request.params))
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ExecutionResolutionError(_format_issues(process.id, variant.variant_id, errors))

    try:
        lease = provider.acquire(variant.engine, request.compute)
    except EngineUnavailableError as error:
        raise ExecutionResolutionError(
            f"variant {process.id!r}/{variant.variant_id!r} requires engine "
            f"{variant.engine.kind!r} {variant.engine.engine_id!r} "
            f"[{variant.engine.min_version!r}, {variant.engine.max_version!r}]: {error}"
        ) from error

    try:
        if adapter is None:
            config = None
            if process.execution_kind == "field":
                # The canonical parameter model is authoritative. A later field
                # step may omit substrateDepth and inherit the canonical lattice
                # from the existing snapshot instead of from the raw step.
                payload = _cellset_payload_from_params(request.params)
                stored = CellSetConfig.from_state(request.state.fields) if request.state.fields is not None else None
                if payload is not None:
                    if payload.get("substrateDepth") is None:
                        if stored is None:
                            raise ExecutionResolutionError("the first field step must declare cellSet.substrateDepth")
                        payload["substrateDepth"] = stored.substrate_depth
                    try:
                        config = CellSetConfig.from_mapping(payload)
                    except CellSetBackendError as error:
                        raise ExecutionResolutionError(str(error)) from error
                elif stored is not None:
                    config = stored
                else:
                    raise ExecutionResolutionError("the first field step must declare cellSet.substrateDepth")
            selected_adapter = DefaultStateAdapter(
                cellset_backend=getattr(lease, "cellset_backend", None),
                context=request.context,
                cellset_config=config,
            )
            missing = sorted(
                kind for kind in capabilities.reads if not _state_provides(request.state, kind, selected_adapter)
            )
            if missing:
                raise ExecutionResolutionError(
                    f"variant {process.id!r}/{variant.variant_id!r} requires state kinds {missing}, "
                    f"but the process state does not provide them"
                )

        lease.state_backend.validate(request.state)
        material_registry = lease.state_backend.material_registry
        if material_registry is not None:
            missing = missing_material_names(request.params, material_registry)
            if missing:
                raise ExecutionResolutionError(
                    f"variant {process.id!r}/{variant.variant_id!r} references materials "
                    f"{list(missing)!r} that are absent from the engine's material registry"
                )
    except StateBackendError as error:
        lease.release()
        raise ExecutionResolutionError(
            f"variant {process.id!r}/{variant.variant_id!r} cannot use state backend "
            f"{lease.state_backend.id!r}: {error}"
        ) from error
    except BaseException:
        lease.release()
        raise

    try:
        contract = resolve_state_contract(capabilities.state_profile, request.context, request.state, lease)
    except ExecutionResolutionError:
        lease.release()
        raise

    return ExecutionPlan(
        request=request,
        process=process,
        variant=variant,
        engine=lease,
        state_adapter=selected_adapter,
        state_contract=contract,
        process_version=resolved.process_version,
        variant_version=resolved.variant_version,
        implementation_hash=resolved.implementation_hash,
        engine_version=lease.engine_version,
        engine_fingerprint=lease.fingerprint,
    )


def resolve_state_contract(
    profile: StateProfile | None,
    context: ProcessContext,
    state: ProcessState,
    lease: EngineLease,
) -> StateContract:
    """Resolve and validate the variant's state profile against the backend.

    No conversion is executed here. The contract only records which conversion
    policy the platform may use and rejects a request the backend cannot serve.
    """
    backend = lease.state_backend
    capabilities = backend.capabilities
    declared = profile or StateProfile()
    conversions = tuple(declared.conversions)
    advertised = set(capabilities.conversions)
    cellset_backend = getattr(lease, "cellset_backend", None)
    if cellset_backend is not None:
        advertised.update(cellset_backend.capabilities.conversions)
    missing_conversions = sorted(set(conversions) - advertised)
    if missing_conversions:
        raise ExecutionResolutionError(
            f"state backend {backend.id!r} does not provide conversions {missing_conversions!r}; "
            f"advertised conversions are {sorted(advertised)!r}"
        )

    if declared.required_materials:
        registry = backend.material_registry
        if registry is not None:
            try:
                registry.require(declared.required_materials)
            except MaterialRegistryError as error:
                raise ExecutionResolutionError(
                    f"state backend {backend.id!r} does not know required materials "
                    f"{list(declared.required_materials)!r}: {error}"
                ) from error
        else:
            available_materials = set(state.materials.names.values())
            missing_materials = sorted(set(declared.required_materials) - available_materials)
            if missing_materials:
                raise ExecutionResolutionError(
                    f"process state is missing required materials {missing_materials!r}; "
                    f"available material names are {sorted(available_materials)!r}"
                )

    missing_persistence = sorted(set(declared.persistence) - set(capabilities.persistence))
    if missing_persistence:
        raise ExecutionResolutionError(
            f"state backend {backend.id!r} cannot persist {missing_persistence!r}; "
            f"advertised persistence is {sorted(capabilities.persistence)!r}"
        )

    return StateContract(
        backend_id=backend.id,
        backend_version=backend.version,
        dimension=context.dimension,
        grid_delta=context.grid_delta,
        conversions=conversions,
        required_materials=declared.required_materials,
        persistence=declared.persistence,
    )


def execute_plan(plan: ExecutionPlan) -> ExecutionOutcome:
    """Run one resolved plan end to end.

    This is the single execution entry point: it uses only the plan's own
    request (state, params, context), passes the acquired engine lease to the
    variant explicitly, materializes any derived representation, and commits
    ``result.delta``. It deliberately does not checkpoint or roll back;
    variants that declare ``mutates_input=True`` must be checkpointed by the
    caller before this function is invoked.
    """
    request = plan.request
    if not isinstance(request.params, plan.process.params_model):
        raise ExecutionResolutionError(
            f"process {plan.process.id!r} version {plan.process_version!r} expects params "
            f"{plan.process.params_model.__name__}, got {type(request.params).__name__}"
        )

    prepared = plan.state_adapter.prepare(request.state, plan.variant.capabilities)
    result = plan.variant.variant.execute(prepared, request.params, request.context, plan.engine)
    if not isinstance(result, StepResult):
        raise ExecutionResolutionError(
            f"variant {plan.process.id!r}/{plan.variant.variant_id!r} returned "
            f"{type(result).__name__}, expected StepResult"
        )
    committed = plan.state_adapter.commit(prepared, result.delta, plan.variant.capabilities)
    return ExecutionOutcome(plan=plan, state=committed, result=result)


def run_step(
    registry: ProcessRegistry,
    provider: EngineProvider,
    process_id: str,
    variant_id: str,
    params: BaseModel,
    context: ProcessContext,
    state: ProcessState,
    *,
    expected_process_version: str | None = None,
    expected_variant_version: str | None = None,
    adapter: StateAdapter | None = None,
    compute: ComputePolicy | None = None,
) -> ExecutionOutcome:
    """Resolve and execute one step in a single call.

    This is the convenient entry point for callers that do not need to inspect
    or cache the :class:`ExecutionPlan`. Admission checks are unchanged because
    resolution goes through :func:`resolve_execution`.
    """
    request = ExecutionRequest(
        process_id=process_id,
        variant_id=variant_id,
        params=params,
        context=context,
        state=state,
        expected_process_version=expected_process_version,
        expected_variant_version=expected_variant_version,
        compute=compute,
    )
    return execute_plan(resolve_execution(request, registry, provider, adapter=adapter))


def _cellset_payload_from_params(params: BaseModel) -> dict[str, Any] | None:
    """Return the raw canonical cell-set payload carried by typed params."""
    cell_set = getattr(params, "cellSet", None)
    if cell_set is None:
        return None
    if hasattr(cell_set, "model_dump"):
        return cell_set.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(cell_set, Mapping):
        return dict(cell_set)
    return None


def _check_geometry_grid(state: ProcessState, context: ProcessContext) -> None:
    """Reject geometry that does not match the request's dimension/grid."""
    geometry = state.geometry
    if geometry is None:
        return
    if int(geometry.dimension) != int(context.dimension):
        raise ExecutionResolutionError(
            f"state geometry is {geometry.dimension}D but the request context is {context.dimension}D"
        )
    tolerance = 1e-9 * max(1.0, abs(float(context.grid_delta)))
    if abs(float(geometry.grid_delta) - float(context.grid_delta)) > tolerance:
        raise ExecutionResolutionError(
            f"state geometry grid delta {geometry.grid_delta!r} does not match "
            f"the request context grid delta {context.grid_delta!r}"
        )


def _state_provides(state: ProcessState, kind: StateKind, adapter: StateAdapter) -> bool:
    """Return whether ``state`` plus ``adapter`` can supply ``kind``.

    ``layers`` is stored-only. ``voxels`` is derived from geometry by the
    default adapter. ``fields`` is derived when the adapter has a cell-set
    backend; without one it is stored-only.
    """
    if state.has(kind):
        return True
    if kind not in adapter.provides:
        return False
    return state.has("geometry")


def _format_issues(process_id: str, variant_id: str, issues: list[Issue]) -> str:
    details = "; ".join(f"{issue.path}: {issue.message}" if issue.path else issue.message for issue in issues)
    return f"variant {process_id!r}/{variant_id!r} rejected the request: {details}"
