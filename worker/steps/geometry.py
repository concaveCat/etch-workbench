"""Native geometry operations (extrude, boolean, mask, ...)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from worker.steps.context import RunContext, simulate_module

V2_PLUGIN_SHADOW_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW"
V2_PLUGIN_APPLY_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY"


def _v2_plugin_shadow_enabled() -> bool:
    return os.environ.get(V2_PLUGIN_SHADOW_ENV) == "1"


def _v2_plugin_apply_enabled() -> bool:
    return os.environ.get(V2_PLUGIN_APPLY_ENV) == "1"


def _run_v2_geometry_shadow(
    ctx: RunContext,
    step: dict[str, Any],
    shadow_domain: Any,
    registry: Any,
    previous_tops: Mapping[str, Any] | None,
    assets: Mapping[str, Any] | None,
) -> None:
    """Run one geometry step through the v2 bridge and compare it with the worker.

    The comparison event is written before a mismatch raises, so a failed run
    keeps the digests, plan identity, and method records that explain it.
    """
    from worker.process_models_v2 import V2ProfileUnavailable, compare_domains, run_v2_step

    try:
        outcome = run_v2_step(
            ctx,
            step,
            shadow_domain,
            registry=registry,
            previous_tops=previous_tops,
            assets=assets,
        )
    except V2ProfileUnavailable as error:
        ctx.event({"type": "v2-plugin-shadow-skipped", "stepId": step["id"], "reason": str(error)})
        return
    shadow_handle = outcome.state.geometry.handle
    comparison = compare_domains(ctx.domain, shadow_handle, int(ctx.project["dimension"]))
    plan_identity = outcome.plan.identity()
    ctx.event(
        {
            "type": "v2-plugin-shadow",
            "stepId": step["id"],
            **comparison,
            "v2Plan": plan_identity,
            "v2Metadata": outcome.metadata,
            "v2Warnings": outcome.warnings,
        }
    )
    if not comparison["matched"]:
        raise RuntimeError(
            f"v2 geometry shadow for step {step['id']!r} did not match the worker path: "
            + "; ".join(comparison["reasons"])
        )
    if not _v2_plugin_apply_enabled():
        return
    ctx.domain = shadow_handle
    ctx.event(
        {
            "type": "v2-plugin-applied",
            "stepId": step["id"],
            "oldDigest": comparison["oldDigest"],
            "newDigest": comparison["newDigest"],
            "v2Plan": plan_identity,
            "v2Metadata": outcome.metadata,
        }
    )


def run_geometry_step(ctx: RunContext, index: int, step: dict[str, Any]) -> dict[str, Any]:
    simulate = simulate_module()
    if os.environ.get("VIENNAPS_WORKBENCH_V2_EXECUTION") == "1":
        from process_models.execution import resolve_dispatch
        from worker.v2_execution import ensure_session, run_v2_claimed_step

        session = ensure_session(ctx)
        decision = resolve_dispatch(ctx.project, step, registry=session.registry)
        if decision.kind == "invalid":
            raise RuntimeError(
                f"v2 execution rejected geometry step {step['id']!r}: {decision.error or decision.reason}"
            )
        if decision.kind == "geometry":
            return run_v2_claimed_step(ctx, index, step, decision, session.registry)
        if decision.kind in ("process", "field"):
            raise RuntimeError(f"step {step['id']!r} was dispatched as {decision.kind} but reached run_geometry_step")
        ctx.event({"type": "v2-execution-fallback", "stepId": step["id"], "reason": decision.reason})
    shadow_domain = None
    v2_registry = None
    if _v2_plugin_shadow_enabled() or _v2_plugin_apply_enabled():
        from process_models.execution import resolve_dispatch
        from worker.v2_execution import ensure_session

        v2_registry = ensure_session(ctx).registry
        decision = resolve_dispatch(ctx.project, step, registry=v2_registry)
        if decision.kind == "invalid":
            raise RuntimeError(
                f"v2 geometry dispatch rejected step {step['id']!r}: {decision.error or decision.reason}"
            )
        if decision.kind == "geometry":
            shadow_domain = ctx.ps.Domain(ctx.domain)
        else:
            ctx.event(
                {
                    "type": "v2-plugin-shadow-skipped",
                    "stepId": step["id"],
                    "reason": decision.reason,
                }
            )
    previous_tops = ctx.snapshots[-1].get("materialTops")
    actual_parameters = simulate.apply_geometry_operation(
        ctx.ps, ctx.domain, ctx.project, step, previous_tops=previous_tops, config=ctx.config, run_dir=ctx.run_dir
    )
    if shadow_domain is not None:
        from worker.v2_execution import geometry_assets

        _run_v2_geometry_shadow(ctx, step, shadow_domain, v2_registry, previous_tops, geometry_assets(ctx, step))
    ctx.event(
        {
            "type": "step-configured",
            "stepId": step["id"],
            "operation": step["model"],
            "model": step["model"],
            "noOp": False,
            "actualEngine": "not-applicable",
            "engineSelectionReason": "native geometry operation; no process engine or physical duration",
            "actualSeed": None,
            "actualParameters": actual_parameters,
        }
    )
    metadata = ctx.save_snapshot(
        ctx.domain,
        step_id=step["id"],
        step_name=step["name"],
        step_index=index,
        requested=ctx.project,
        step=step,
        previous_tops=previous_tops,
        defer_payload=simulate.LAZY_SNAPSHOTS,
    )
    metadata["nativeState"] = ctx.save_native_state(ctx.domain, step["id"])
    ctx.actual_execution.append({"stepId": step["id"], "engine": "not-applicable", "seed": None})
    if os.environ.get("VIENNAPS_WORKBENCH_V2_EXECUTION") == "1":
        from worker.v2_state import WorkerV2State

        WorkerV2State.invalidate(ctx)
    return metadata
