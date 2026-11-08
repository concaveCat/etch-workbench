"""Worker-side bridge to the generic v2 ``FlowExecutor``.

A v2 plugin declares one optional :class:`~process_models.v2.contract.ProcessSpec`
hook: ``parse_step(project, step)``. It is the only plugin knowledge this
module has: nothing here names a concrete process, and adding a process never
touches the worker.

:func:`find_v2_process` asks the plugin registry which process claims a raw
worker step; :func:`run_v2_step` turns the worker's live ``viennaps`` module,
project dict, step dict, and ``ps.Domain`` into a one-step v2 flow and returns
the committed :class:`~process_models.v2.resolver.ExecutionOutcome`.
:class:`WorkerV2Session` keeps the registry, provider, and native state
backends alive across the steps of one run; it still executes and persists
one step at a time, so per-step snapshots, GPU teardown, and replay artifacts
are unchanged. :func:`compare_domains` is the generic digest/material-map
comparison the shadow hook uses to check the two paths agree.

Import safety
-------------
``process_models.v2`` is imported at module top; it never imports the worker.
The worker's ``simulate`` module is resolved lazily inside
:func:`compare_domains` through :func:`worker.steps.context.simulate_module`,
which returns the already-loaded copy even when the bounded child runs
``simulate.py`` as ``__main__``. That keeps this module importable from
``worker.steps.process`` without creating a circular import or a second copy of
the worker's module-level native state.

Persistence
-----------
Every call writes the executor's artifacts below
``<run_dir>/v2/<step_id>/`` (``steps/<step_id>/{state,outputs,plan}.json``,
optionally ``geometry.vpsd``, plus ``flow.json``). The flow runs on a
checkpointed copy of the handle because the native variant declares
``mutates_input``; the caller's domain is left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from process_models.engine_plugins import EngineContext, load_engine_registry
from process_models.execution import load_execution_registry
from process_models.plugins import load_registry
from process_models.v2.contract import ProcessContext, ProcessSpec
from process_models.v2.dispatch import normalize_plugin_step
from process_models.v2.engine import ComputePolicy, EngineProvider
from process_models.v2.engines.viennaps import material_map
from process_models.v2.executor import FlowSession, FlowStep
from process_models.v2.geometry import register_geometry
from process_models.v2.registry import ProcessRegistry
from process_models.v2.resolver import ExecutionOutcome
from process_models.v2.state import LevelSetState, MaterialState, ProcessState
from process_models.v2.storage import StateStore

__all__ = [
    "V2ProfileUnavailable",
    "WorkerV2Session",
    "build_worker_v2_session",
    "compare_domains",
    "compute_policy_for_step",
    "ensure_worker_v2_session",
    "find_v2_process",
    "material_order",
    "run_v2_step",
]

DEFAULT_ENGINE_FINGERPRINT = "worker-v2-plugin"
_CPU_KERNELS = frozenset({"CPU_DISK", "CPU_TRIANGLE"})


class V2ProfileUnavailable(RuntimeError):
    """The v2 bridge cannot honour the engine profile resolved for a step.

    The shadow hook catches this and records an auditable skip. It must never
    silently run the step on a different backend/kernel.
    """


def compute_policy_for_step(ctx: Any, step: Mapping[str, Any]) -> ComputePolicy:
    """Return the concrete v2 compute policy for one worker step.

    ``ctx.step_engines`` is populated by the worker before steps run. A step
    without an entry (for example a non-flux model) uses the CPU_DISK pin the
    current plugins declare. GPU kernels carry the worker-selected device so
    the provider can match the exact GPU lease.
    """
    solver = ctx.project.get("solver", {}) if isinstance(getattr(ctx, "project", None), Mapping) else {}
    if solver.get("engineId") is not None:
        kernel = solver.get("engineKernel")
        if not isinstance(kernel, str) or not kernel:
            raise V2ProfileUnavailable("solver.engineId requires solver.engineKernel for v2 execution")
        backend = solver.get("engineBackend") or ("gpu" if kernel.startswith("GPU") else "cpu")
        selected = getattr(ctx, "selected_gpu_device", None)
        device = (
            str(selected["uuid"])
            if backend == "gpu" and isinstance(selected, Mapping) and selected.get("uuid")
            else None
        )
        return ComputePolicy(backend=str(backend), kernel=kernel, device=device)
    engine = getattr(ctx, "step_engines", {}).get(step["id"])
    if engine in (None, "not-applicable"):
        # Let the variant's declared profiles choose. Native variants declare
        # CPU_DISK first, so their historical default is unchanged; an
        # external engine can declare its own kernel without a worker white
        # list. A profile that advertises no CPU backend still fails closed in
        # StaticEngine.acquire().
        return ComputePolicy(backend="auto")
    if engine in {"CPU_DISK", "CPU_TRIANGLE"}:
        return ComputePolicy(backend="cpu", kernel=str(engine))
    if engine in {"GPU_DISK", "GPU_TRIANGLE"}:
        selected = getattr(ctx, "selected_gpu_device", None)
        device = selected.get("uuid") if isinstance(selected, Mapping) else None
        return ComputePolicy(backend="gpu", kernel=str(engine), device=device)
    # A custom engine may expose a non-native kernel through a project/worker
    # policy. The provider still performs strict capability matching; this
    # function must not reject an unknown name before the provider sees it.
    selected = getattr(ctx, "selected_gpu_device", None)
    device = str(selected["uuid"]) if isinstance(selected, Mapping) and selected.get("uuid") else None
    backend = "gpu" if str(engine).startswith("GPU") else "cpu"
    return ComputePolicy(backend=backend, kernel=str(engine), device=device if backend == "gpu" else None)


def material_order(domain: Any) -> tuple[int, ...]:
    """Return the native material IDs in level-set order."""
    return tuple(int(material_id) for material_id in material_map(domain))


def find_v2_process(
    project: Mapping[str, Any], step: Mapping[str, Any], *, registry: ProcessRegistry | None = None
) -> ProcessSpec | None:
    """Return the single registered process whose ``parse_step`` accepts ``step``.

    ``None`` means no plugin claims the step. An ambiguous match raises
    ``ValueError`` from the registry rather than guessing. Pass an existing
    ``registry`` to avoid rebuilding and re-verifying the lock twice. The
    returned spec is the one whose ``parse_step`` produced the parameters;
    :func:`run_v2_step` resolves the step again (one parse per call) because it
    needs the parameters, not just the identity.
    """
    if registry is None:
        registry = load_registry()
    resolved = registry.resolve_step(project, step)
    return None if resolved is None else resolved[0]


@dataclass(frozen=True)
class WorkerV2Session:
    """Run-scoped v2 objects reused by every claimed worker step.

    The session deliberately does **not** batch steps.  It keeps the
    registry, engine provider, native state backend, and cell-set backend
    alive across steps, while :meth:`run_step` still executes and persists
    exactly one step.  That preserves the worker's per-step snapshot / event /
    GPU-teardown boundaries and the per-step replay layout under
    ``<run_dir>/v2/<step_id>``.
    """

    registry: ProcessRegistry
    provider: EngineProvider
    ps_version: str
    fingerprint: str
    device_uuid: str | None
    run_dir: Path
    flow: FlowSession

    @property
    def key(self) -> tuple[Any, ...]:
        """Identity of the run-scoped objects this session may reuse."""
        return (
            self.run_dir,
            self.ps_version,
            self.fingerprint,
            self.device_uuid,
        )

    def run_step(
        self,
        ctx: Any,
        step: dict[str, Any],
        domain: Any,
        *,
        allow_gpu: bool = False,
        previous_tops: Mapping[str, Any] | None = None,
        assets: Mapping[str, Any] | None = None,
        state: ProcessState | None = None,
        field_sequence_end: bool = True,
    ) -> ExecutionOutcome:
        """Execute and persist one step; never batch the surrounding flow."""
        step = dict(normalize_plugin_step(step))
        resolved = self.registry.resolve_step(ctx.project, step)
        if resolved is None:
            raise RuntimeError(f"no registered v2 process matches step {step['id']!r}")
        spec, params = resolved

        variant_id = self.registry.default_variant_id(spec.id)
        policy = compute_policy_for_step(ctx, step)
        variant = self.registry.get_variant(spec.id, variant_id).variant
        if variant.engine.profiles and not any(
            (policy.backend == "auto" or profile.backend == policy.backend)
            and (policy.kernel is None or profile.kernel == policy.kernel)
            for profile in variant.engine.profiles
        ):
            raise V2ProfileUnavailable(
                f"step {step['id']!r} resolved to {policy.backend}/{policy.kernel!r}, but "
                f"variant {spec.id!r}/{variant_id!r} declares profiles "
                f"{[profile.identity() for profile in variant.engine.profiles]!r}"
            )
        if policy.backend == "gpu":
            if not allow_gpu:
                raise V2ProfileUnavailable(
                    f"step {step['id']!r} resolved to {policy.backend}/{policy.kernel!r}; "
                    "set VIENNAPS_WORKBENCH_V2_PLUGIN_GPU=1 to enable the v2 GPU execution path"
                )
            selected = getattr(ctx, "selected_gpu_device", None)
            readback = getattr(ctx, "cuda_readback", None)
            if not isinstance(selected, Mapping) or not isinstance(readback, Mapping):
                raise V2ProfileUnavailable(
                    f"step {step['id']!r} requires a worker-selected CUDA device and readback for GPU execution"
                )
            if selected.get("uuid") != readback.get("uuid"):
                raise V2ProfileUnavailable(
                    f"step {step['id']!r} CUDA readback UUID {readback.get('uuid')!r} "
                    f"does not match selected device {selected.get('uuid')!r}"
                )
            try:
                from worker.engine_policy import verify_worker_engine_evidence

                verify_worker_engine_evidence(ctx.project, step, str(policy.kernel))
            except RuntimeError as error:
                raise V2ProfileUnavailable(str(error)) from error
        elif policy.kernel not in _CPU_KERNELS and not variant.engine.profiles:
            # A custom engine may use its own kernel name. If the variant
            # declares profiles, the profile check above already admitted it;
            # only the legacy no-profile path keeps the native CPU guard.
            raise V2ProfileUnavailable(
                f"step {step['id']!r} resolved to {policy.backend}/{policy.kernel!r}; "
                f"the v2 worker bridge only provides {sorted(_CPU_KERNELS)!r}"
            )

        run_assets = dict((getattr(ctx, "config", {}) or {}).get("csvAssets", {}))
        if assets:
            run_assets.update(assets)
        context = ProcessContext(
            dimension=int(ctx.project["dimension"]),
            grid_delta=float(ctx.project["domain"]["gridDelta"]),
            project=ctx.project,
            step=step,
            seed=getattr(ctx, "run_seed", None),
            assets=run_assets,
            previous_tops=previous_tops or {},
        )
        if state is None:
            order = material_order(domain)
            state = ProcessState(
                geometry=LevelSetState(
                    handle=domain,
                    dimension=context.dimension,
                    grid_delta=context.grid_delta,
                ),
                materials=MaterialState(order=order),
            )
        return self.flow.run_step(
            state,
            FlowStep(
                step_id=step["id"],
                process_id=spec.id,
                variant_id=variant_id,
                params=params,
                context=context,
                compute=policy,
            ),
            field_sequence_end=field_sequence_end,
        )


def _ctx_session_key(ctx: Any) -> tuple[Any, ...]:
    selected = getattr(ctx, "selected_gpu_device", None)
    device_uuid = selected.get("uuid") if isinstance(selected, Mapping) else None
    return (
        Path(ctx.run_dir),
        str(ctx.ps_version),
        str(getattr(ctx, "native_engine_fingerprint", None) or DEFAULT_ENGINE_FINGERPRINT),
        device_uuid,
    )


def build_worker_v2_session(
    ctx: Any,
    registry: ProcessRegistry | None = None,
) -> WorkerV2Session:
    """Build the run-scoped v2 objects exactly once per worker run."""
    if registry is None:
        registry = load_execution_registry()
    else:
        register_geometry(registry)

    selected_device = getattr(ctx, "selected_gpu_device", None)
    fingerprint = str(ctx.native_engine_fingerprint or DEFAULT_ENGINE_FINGERPRINT)
    run_dir = Path(ctx.run_dir)
    engine_registry = load_engine_registry()
    provider = engine_registry.provider(
        EngineContext(
            project=ctx.project,
            run_dir=run_dir,
            selected_device=selected_device if isinstance(selected_device, Mapping) else None,
            config=getattr(ctx, "config", {}) or {},
            fingerprint=fingerprint,
        )
    )
    flow = FlowSession(
        registry,
        provider,
        store_factory=lambda step_id: StateStore(run_dir / "v2" / step_id),
    )
    return WorkerV2Session(
        registry=registry,
        provider=provider,
        ps_version=str(ctx.ps_version),
        fingerprint=fingerprint,
        device_uuid=(
            str(selected_device["uuid"])
            if isinstance(selected_device, Mapping) and selected_device.get("uuid")
            else None
        ),
        run_dir=run_dir,
        flow=flow,
    )


def ensure_worker_v2_session(
    ctx: Any,
    registry: ProcessRegistry | None = None,
) -> WorkerV2Session:
    """Return the cached run session, rebuilding only when its identity changes."""
    existing = getattr(ctx, "v2_session", None)
    if existing is not None:
        if registry is not None and existing.registry is not registry:
            raise RuntimeError(
                "v2 session registry differs from the registry used for dispatch; use session.registry for both"
            )
        if existing.key == _ctx_session_key(ctx):
            return existing
    session = build_worker_v2_session(ctx, registry)
    ctx.v2_session = session
    return session


def run_v2_step(
    ctx: Any,
    step: dict[str, Any],
    domain: Any,
    *,
    registry: ProcessRegistry | None = None,
    allow_gpu: bool = False,
    previous_tops: Mapping[str, Any] | None = None,
    assets: Mapping[str, Any] | None = None,
    state: ProcessState | None = None,
    field_sequence_end: bool = True,
    session: WorkerV2Session | None = None,
) -> ExecutionOutcome:
    """Run one worker step through the v2 registry and return its outcome.

    ``ctx`` is a :class:`worker.steps.context.RunContext` (or any object with
    ``project``, ``ps``, ``ps_version``, ``native_engine_fingerprint``, and
    ``run_dir``). If ``ctx.v2_session`` exists, the run-scoped registry,
    provider, and native backends are reused; otherwise they are built once
    and cached. The step is parsed exactly once through
    ``registry.resolve_step``. Only the last step of the one-step flow is
    returned, and that step still writes its own ``<run_dir>/v2/<step_id>``
    replay artifacts.

    The native variant mutates its input handle, so the executor checkpoints a
    copy through the ViennaPS state backend first: ``domain`` is never modified
    and ``outcome.state.geometry.handle`` is the processed copy.

    GPU execution is explicitly opt-in via ``allow_gpu`` (the worker hook sets
    it from ``VIENNAPS_WORKBENCH_V2_PLUGIN_GPU``). A GPU step requires the
    worker's selected device and CUDA readback, and ``GPU_TRIANGLE`` is gated by
    the same capability evidence as the worker path. A step the bridge cannot
    honour raises :class:`V2ProfileUnavailable` instead of silently running on
    CPU.
    """
    if session is None:
        session = getattr(ctx, "v2_session", None)
    if session is None:
        session = build_worker_v2_session(ctx, registry)
    elif registry is not None and session.registry is not registry:
        raise RuntimeError(
            "run_v2_step received a registry that is not the session's registry; "
            "use session.registry so admission and execution see the same plugin set"
        )
    return session.run_step(
        ctx,
        step,
        domain,
        allow_gpu=allow_gpu,
        previous_tops=previous_tops,
        assets=assets,
        state=state,
        field_sequence_end=field_sequence_end,
    )


def compare_domains(old_domain: Any, new_domain: Any, dimension: int) -> dict[str, Any]:
    """Compare two domains by exposed-surface digest and material map.

    The result is a plain JSON-safe dict for run records and applies to any
    native level-set geometry. A mismatch is data, not an exception: the caller
    decides whether to warn, fail, or accept the difference. Point/cell counts
    are reported for diagnosis; the digest already covers them, so they do not
    add separate mismatch reasons.
    """
    from worker.steps.context import simulate_module

    simulate = simulate_module()
    old_surface = simulate.surface_state(old_domain, dimension)
    new_surface = simulate.surface_state(new_domain, dimension)
    old_map = simulate.material_map(old_domain)
    new_map = simulate.material_map(new_domain)

    reasons: list[str] = []
    if old_surface["digest"] != new_surface["digest"]:
        reasons.append(f"surface digest differs: {old_surface['digest']} != {new_surface['digest']}")
    if old_map != new_map:
        reasons.append(f"material map differs: {old_map} != {new_map}")

    return {
        "matched": not reasons,
        "oldDigest": old_surface["digest"],
        "newDigest": new_surface["digest"],
        "oldMaterialMap": old_map,
        "newMaterialMap": new_map,
        "oldPointCount": int(old_surface["pointCount"]),
        "newPointCount": int(new_surface["pointCount"]),
        "oldCellCount": int(old_surface["cellCount"]),
        "newCellCount": int(new_surface["cellCount"]),
        "reasons": reasons,
    }
