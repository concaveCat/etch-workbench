"""Native process steps: build and apply a ViennaPS Process.

This is the branch that used to be inline in ``worker/simulate.py::execute``.
It owns every native object it creates and returns them to the loop so the
caller keeps the exact same lifetime for the per-step GPU teardown.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from worker.steps.context import RunContext, simulate_module

V2_PLUGIN_SHADOW_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW"
V2_PLUGIN_APPLY_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY"
V2_PLUGIN_GPU_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_GPU"


def _v2_plugin_shadow_enabled() -> bool:
    """The plugin shadow hook is strictly opt-in; the default run must not change."""
    return os.environ.get(V2_PLUGIN_SHADOW_ENV) == "1"


def _v2_plugin_gpu_enabled() -> bool:
    """GPU v2 execution is a separate opt-in even when shadow/apply is on."""
    return os.environ.get(V2_PLUGIN_GPU_ENV) == "1"


def _v2_plugin_apply_enabled() -> bool:
    """Validation-only apply mode: also strictly opt-in.

    With this flag the matched v2 domain replaces ``ctx.domain`` after the
    shadow comparison, so snapshots, native state and replay can be validated
    against the v2 result. It is *not* a production cutover: the default path is
    untouched and an apply always implies the shadow comparison.
    """
    return os.environ.get(V2_PLUGIN_APPLY_ENV) == "1"


def _v2_plugin_skip_reason(step: dict[str, Any]) -> str:
    """Return the audit reason for a step no registered plugin claims.

    The hook runs for every step while a flag is set, not only for the models
    that have a plugin today, so the reason names the dispatch fields the
    registered ``parse_step`` hooks inspect instead of assuming one model.
    A skipped step is auditable; a step is never silently unvalidated.
    """
    fields = ", ".join(f"{name}={step.get(name)!r}" for name in ("model", "operation", "rateMode", "isotropicMethod"))
    return f"no registered v2 plugin process matches this step ({fields})"


def _run_v2_plugin_shadow(
    ctx: RunContext,
    step: dict[str, Any],
    shadow_domain: Any,
    registry: Any,
) -> None:
    """Run the v2 path on ``shadow_domain``, compare, and optionally apply.

    The comparison event is written before a mismatch is raised so the failed
    run keeps the digests, material maps and plan identity that explain the
    divergence. Only a matched domain is ever assigned to ``ctx.domain``. The
    caller passes the registry it already loaded for dispatch, so the bridge
    resolves the step without loading and verifying the lock a second time.
    """
    from worker.process_models_v2 import V2ProfileUnavailable, compare_domains, run_v2_step

    try:
        outcome = run_v2_step(
            ctx,
            step,
            shadow_domain,
            registry=registry,
            allow_gpu=_v2_plugin_gpu_enabled(),
        )
    except V2ProfileUnavailable as error:
        ctx.event(
            {
                "type": "v2-plugin-shadow-skipped",
                "stepId": step["id"],
                "reason": str(error),
            }
        )
        return
    shadow_geometry = getattr(outcome.state, "geometry", None)
    shadow_handle = getattr(shadow_geometry, "handle", None)
    if shadow_handle is None:
        raise RuntimeError(f"v2 plugin shadow for step {step['id']!r} returned no geometry state")
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
            f"v2 plugin shadow for step {step['id']!r} did not match the worker path: "
            + "; ".join(comparison["reasons"])
        )
    if not _v2_plugin_apply_enabled():
        return
    # Validation-only apply: from here on the snapshots, native state and
    # replay records describe the v2 domain. The old process/model handles stay
    # referenced by ProcessStepResult; the worker path still owns this step's
    # GPU teardown, including when the v2 GPU path ran on the same device.
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


@dataclass
class ProcessStepResult:
    metadata: dict[str, Any]
    process: Any | None = None
    model: Any | None = None


def run_process_step(ctx: RunContext, index: int, step: dict[str, Any]) -> ProcessStepResult:
    simulate = simulate_module()
    # Plugin-owned steps have no legacy branch: model="plugin" must always go
    # through the v2 dispatch/registry path, even when native steps still use
    # the opt-in legacy default.
    if (
        os.environ.get("VIENNAPS_WORKBENCH_V2_EXECUTION") == "1"
        or step.get("_pluginStep")
        or step.get("model") == "plugin"
    ):
        from process_models.v2.dispatch import dispatch_step
        from worker.v2_execution import ensure_session, run_v2_claimed_step

        session = ensure_session(ctx)
        decision = dispatch_step(session.registry, ctx.project, step)
        if decision.kind == "invalid":
            raise RuntimeError(
                f"v2 execution rejected process step {step['id']!r}: {decision.error or decision.reason}"
            )
        if decision.kind == "process":
            return ProcessStepResult(metadata=run_v2_claimed_step(ctx, index, step, decision, session.registry))
        if decision.kind in ("field", "geometry"):
            raise RuntimeError(f"step {step['id']!r} was dispatched as {decision.kind} but reached run_process_step")
        if step.get("_pluginStep"):
            raise RuntimeError(
                f"plugin step {step['id']!r} references pluginId {step.get('model')!r}, "
                f"but no installed process plugin claims it: {decision.reason}"
            )
        ctx.event({"type": "v2-execution-fallback", "stepId": step["id"], "reason": decision.reason})
    shadow_domain = None
    v2_registry = None
    if _v2_plugin_shadow_enabled() or _v2_plugin_apply_enabled():
        from process_models.plugins import load_registry
        from worker.process_models_v2 import find_v2_process

        # One discovery/verification pass per step, shared by dispatch and the
        # bridge instead of being repeated for each lookup.
        v2_registry = load_registry()
        spec = find_v2_process(ctx.project, step, registry=v2_registry)
        if spec is not None:
            # Deep-copy before the worker path mutates ctx.domain so both
            # branches start from the identical geometry.
            shadow_domain = ctx.ps.Domain(ctx.domain)
        else:
            ctx.event(
                {
                    "type": "v2-plugin-shadow-skipped",
                    "stepId": step["id"],
                    "reason": _v2_plugin_skip_reason(step),
                }
            )
    deposition = step["operation"] == "deposit"
    directional = ctx.project["schemaVersion"] in (5, 6, 7) and step.get("model") == "directional"
    specialized = ctx.project["schemaVersion"] == 7 and step.get("model") == "specialized"
    csv_process = ctx.project["schemaVersion"] == 7 and step.get("model") == "csvProcess"
    rate = None
    signed_material_rates: dict[str, float] = {}
    multi_actual: dict[str, Any] | None = None
    callback_state: dict[str, Any] | None = None
    if deposition:
        if specialized and step.get("nativeModel") == "selectiveEpitaxy":
            # The installed SelectiveEpitaxy binding checks the native
            # enum material identity of its epitaxial top level set.
            deposit_material = ctx.ps.Material.SiGe
        else:
            deposit_material = (
                simulate.material_handle(ctx.ps, step["depositMaterial"])
                if ctx.project["schemaVersion"] >= 3
                else ctx.ps.Material.SiO2
            )
        ctx.domain.duplicateTopLevelSet(deposit_material)
    if os.environ.get("VIENNAPS_DEBUG_NATIVE_CALLBACKS") == "1" and step.get("model") == "multiParticle":
        debug_mesh = ctx.domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        debug_domain = simulate.native_mesh_report(debug_mesh)
        debug_domain["materialIdsByLevelSetIndex"] = simulate.material_map(ctx.domain)
        debug_domain["levelSetCount"] = int(ctx.domain.getNumberOfLevelSets())
        simulate.atomic_json(ctx.run_dir / f"native-debug-pre-process-{step['id']}.json", debug_domain)
    particle = simulate.uses_flux(step)
    material_mode = ctx.project["schemaVersion"] in (4, 5, 6, 7) and step.get("rateMode") == "material"
    directional_parameters: dict[str, Any] | None = None
    if directional:
        if step.get("rateSets") is not None:
            set_records = simulate.directional_rate_set_records(ctx.project, step)
            model = ctx.ps.DirectionalProcess(simulate.native_rate_sets(ctx.ps, set_records))
            directional_parameters = {
                "mode": "rateSets",
                "nativeOverload": simulate.RATE_SET_OVERLOAD,
                "directionCoordinateMap": "2D [x,h,0] -> native [x,h,0]; 3D [x,y,z] -> native [x,y,z] (z vertical)",
                "rateSetCount": len(set_records),
                "rateSets": set_records,
                "maskMaterials": set_records[0]["maskMaterials"] if set_records else [],
                "maskMaterialIds": set_records[0]["maskMaterialIds"] if set_records else [],
                "nativeProcessBackend": "CPU_DISK pinned; directional model has no stochastic flux engine",
                "raysPerPoint": "not-applicable",
                "randomSeed": "not-applicable",
            }
        elif simulate.directional_material_mode(step):
            # Defense in depth: a hand-edited worker input must never
            # combine the material map with the scalar/rate-set modes.
            conflicting = sorted(
                {
                    name
                    for name, active in (
                        ("rateSets", step.get("rateSets") is not None),
                        ("directionalRateMagnitude", step.get("directionalRateMagnitude") is not None),
                        ("isotropicRateMagnitude", step.get("isotropicRateMagnitude") is not None),
                    )
                    if active
                }
            )
            if conflicting:
                raise RuntimeError("directional materialRates is mutually exclusive with " + ", ".join(conflicting))
            if step.get("calculateVisibility") is False:
                raise RuntimeError(
                    "directional materialRates has no native visibility control; "
                    "calculateVisibility must be omitted or true"
                )
            raw_direction = [float(value) for value in (step.get("direction") or [])]
            if len(raw_direction) != 3:
                raise RuntimeError("directional materialRates requires a 3-component direction")
            length = math.sqrt(sum(value * value for value in raw_direction))
            if not math.isfinite(length) or length <= 1e-12:
                raise RuntimeError("direction must be a finite nonzero vector")
            native_direction = [value / length for value in raw_direction]
            material_record = simulate.directional_material_rate_records(step)
            native_rate_map = {
                simulate.material_handle(ctx.ps, name): pair for name, pair in material_record["native"].items()
            }
            model = ctx.ps.DirectionalProcess(
                native_direction,
                native_rate_map,
                material_record["nativeDefault"][0],
                material_record["nativeDefault"][1],
            )
            directional_parameters = {
                "mode": "materialRates",
                "rateMode": "material",
                "nativeClass": "DirectionalProcess",
                "nativeOverload": simulate.DIRECTIONAL_MATERIAL_OVERLOAD,
                "requestedDirection": raw_direction,
                "nativeUnitDirection": native_direction,
                "directionCoordinateMap": "2D [x,h,0] -> native [x,h,0]; 3D [x,y,z] -> native [x,y,z] (z vertical)",
                "materialRates": material_record["rows"],
                "nativeMaterialRatesByName": {name: list(pair) for name, pair in material_record["native"].items()},
                "nativeMaterialRatesById": {
                    str(row["id"]): [row["nativeDirectionalRate"], row["nativeIsotropicRate"]]
                    for row in material_record["rows"]
                },
                "defaultMaterialRate": material_record["requestedDefault"],
                "defaultMaterialRateOmitted": material_record["defaultOmitted"],
                "nativeDefaultDirectionalRate": material_record["nativeDefault"][0],
                "nativeDefaultIsotropicRate": material_record["nativeDefault"][1],
                "directionalSign": material_record["directionalSign"],
                "isotropicSign": material_record["isotropicSign"],
                "masks": "material rate map only; unlisted materials use the default rate",
                "calculateVisibility": "not-applicable: native material map has no visibility control",
                "nativeProcessBackend": "CPU_DISK pinned; directional model has no stochastic flux engine",
                "raysPerPoint": "not-applicable",
                "randomSeed": "not-applicable",
            }
        else:
            raw_direction = [float(value) for value in step["direction"]]
            length = math.sqrt(sum(value * value for value in raw_direction))
            if not math.isfinite(length) or length <= 1e-12:
                raise RuntimeError("direction must be a finite nonzero vector")
            native_direction = [value / length for value in raw_direction]
            directional_velocity, isotropic_velocity = simulate.signed_directional_rates(step)
            mask_names = simulate.protected_material_names(ctx.project, step)
            mask_materials = [simulate.material_handle(ctx.ps, name) for name in mask_names]
            visibility = bool(step["calculateVisibility"]) if float(step["directionalRateMagnitude"]) > 0 else False
            # ViennaPS 4.6.2 accepts a list under the positional argument
            # named ``maskMaterial``; ``maskMaterials=`` is not a bound
            # keyword, even though a sequence is accepted by the overload.
            model = ctx.ps.DirectionalProcess(
                native_direction, directional_velocity, isotropic_velocity, mask_materials, visibility
            )
            directional_parameters = {
                "mode": "scalar",
                "requestedDirection": raw_direction,
                "nativeUnitDirection": native_direction,
                "directionCoordinateMap": "2D [x,h,0] -> native [x,h,0]; 3D [x,y,z] -> native [x,y,z] (z vertical)",
                "directionalRateMagnitude": float(step["directionalRateMagnitude"]),
                "isotropicRateMagnitude": float(step["isotropicRateMagnitude"]),
                "nativeDirectionalVelocity": directional_velocity,
                "nativeIsotropicVelocity": isotropic_velocity,
                "calculateVisibility": visibility
                if float(step["directionalRateMagnitude"]) > 0
                else "not-applicable: directional rate is zero",
                "maskMaterials": mask_names,
                "maskMaterialIds": [simulate.MATERIAL_IDS[name] for name in mask_names],
                "nativeProcessBackend": "CPU_DISK pinned; directional model has no stochastic flux engine",
                "raysPerPoint": "not-applicable",
                "randomSeed": "not-applicable",
            }
    elif step.get("model") != "multiParticle" and not specialized and not csv_process:
        rate, signed_material_rates = simulate.signed_rates(step)
    particle = simulate.uses_flux(step)
    configured_engine = ctx.step_engines.get(step["id"]) if particle else None
    if configured_engine is None and particle:
        configured_engine = simulate.requested_engine_for_step(ctx.project, step, ctx.step_engines)
        simulate.verify_worker_engine_evidence(ctx.project, step, configured_engine)
    gpu_multi_ion = (
        ctx.project["schemaVersion"] == 7
        and step.get("model") == "multiParticle"
        and configured_engine == "GPU_DISK"
        and sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1
    )
    multi_strategy = ctx.config.get("stepExecutionStrategies", {}).get(step["id"])
    if (
        ctx.project["schemaVersion"] == 7
        and step.get("model") == "multiParticle"
        and str(configured_engine or "").startswith("GPU_")
        and sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1
    ):
        # Defense in depth: a hand-edited worker input must not reach a
        # GPU engine that silently drops the extra ion species.
        policy = simulate.multi_ion_gpu_error()
        if policy is not None:
            raise RuntimeError(policy)
    if gpu_multi_ion and multi_strategy not in {
        simulate.GPU_MULTIPARTICLE_SPLIT_STRATEGY,
        simulate.GPU_MULTIPARTICLE_NATIVE_STRATEGY,
    }:
        raise RuntimeError("GPU MultiParticle input did not pin its multi-ion execution strategy")
    if multi_strategy == simulate.GPU_MULTIPARTICLE_NATIVE_STRATEGY and not ctx.native_engine_fingerprint:
        raise RuntimeError("native multi-ion GPU execution requires a pinned engine build fingerprint")
    gpu_multi_split = gpu_multi_ion and multi_strategy == simulate.GPU_MULTIPARTICLE_SPLIT_STRATEGY
    isotropic_dispatch: dict[str, Any] | None = None
    geometric_isotropic = False
    isotropic_geometric_before: dict[str, Any] | None = None
    isotropic_fields_present = (
        step.get("model") == "isotropic"
        and ctx.project["schemaVersion"] in (4, 5, 6, 7)
        and (step.get("isotropicMethod") is not None or step.get("isotropicTimeStepRatio") is not None)
    )
    if directional:
        pass
    elif specialized:
        model, special_process_parameters, specialized_actual, process_duration = simulate.build_specialized_model(
            ctx.ps,
            ctx.domain,
            ctx.project,
            step,
            lambda name: simulate.material_handle(ctx.ps, name),
            simulate.MATERIAL_IDS,
            flux_engine=configured_engine,
        )
    elif csv_process:
        model, csv_process_actual = simulate.build_csv_process(
            ctx.ps,
            ctx.project,
            step,
            config=ctx.config,
            run_dir=ctx.run_dir,
            material_handle=lambda name: simulate.material_handle(ctx.ps, name),
            material_ids=simulate.MATERIAL_IDS,
        )
        process_duration = float(step["duration"])
    elif step.get("model") == "multiParticle":
        if gpu_multi_split:
            model = None
            multi_actual = simulate.multi_particle_step_summary(step)
        else:
            model, multi_actual, callback_state = simulate.build_multi_particle_model(ctx.ps, step)
    elif particle:
        if material_mode:
            model = ctx.ps.SingleParticleProcess(
                rate=rate,
                stickingProbability=float(step["stickingProbability"]),
                sourceExponent=float(step["sourceExponent"]),
                maskMaterials=[],
            )
            for name, material_rate in signed_material_rates.items():
                model.setMaterialRate(simulate.material_handle(ctx.ps, name), material_rate)
        elif ctx.project["schemaVersion"] >= 3:
            model = ctx.ps.SingleParticleProcess(
                rate=rate,
                stickingProbability=float(step["stickingProbability"]),
                sourceExponent=float(step["sourceExponent"]),
                maskMaterials=[
                    simulate.material_handle(ctx.ps, name)
                    for name in simulate.protected_material_names(ctx.project, step)
                ],
            )
        else:
            model = ctx.ps.SingleParticleProcess(
                rate=rate,
                stickingProbability=float(step["stickingProbability"]),
                sourceExponent=float(step["sourceExponent"]),
                maskMaterial=ctx.ps.Material.Mask,
            )
    else:
        if isotropic_fields_present:
            actual_method, dispatch_reason = simulate.resolve_isotropic_method(ctx.project, step)
        elif step.get("model") == "isotropic" and (
            step.get("isotropicMethod") is not None or step.get("isotropicTimeStepRatio") is not None
        ):
            # Schema-3 projects cannot carry the new fields; a
            # hand-edited input must not silently ignore them.
            raise RuntimeError("isotropicMethod and isotropicTimeStepRatio require a schema-4+ project")
        else:
            actual_method, dispatch_reason = "levelSet", "legacy level-set PDE path"
        if actual_method == "geometric":
            model, isotropic_dispatch, isotropic_geometric_before = simulate.build_isotropic_geometric(
                ctx.ps, ctx.domain, ctx.project, step, reason=dispatch_reason
            )
            geometric_isotropic = True
        elif material_mode:
            isotropic_dispatch = (
                simulate.isotropic_level_set_record(ctx.project, step, dispatch_reason)
                if isotropic_fields_present
                else None
            )
            model = ctx.ps.IsotropicProcess(rate=rate, maskMaterials=[])
            for name, material_rate in signed_material_rates.items():
                model.setMaterialRate(simulate.material_handle(ctx.ps, name), material_rate)
        elif ctx.project["schemaVersion"] >= 3:
            isotropic_dispatch = (
                simulate.isotropic_level_set_record(ctx.project, step, dispatch_reason)
                if isotropic_fields_present
                else None
            )
            model = ctx.ps.IsotropicProcess(
                rate=rate,
                maskMaterials=[
                    simulate.material_handle(ctx.ps, name)
                    for name in simulate.protected_material_names(ctx.project, step)
                ],
            )
        else:
            model = ctx.ps.IsotropicProcess(rate=rate, maskMaterial=ctx.ps.Material.Mask)
    native_duration = (
        process_duration
        if (specialized or csv_process)
        else 0.0
        if geometric_isotropic
        else float(step.get("duration", 0.0))
    )
    process = None if gpu_multi_split else ctx.ps.Process(ctx.domain, model, native_duration)
    # The 4.6.2 Process default attempts CUDA initialization even for
    # isotropic models. Pin a concrete, verified backend before apply.
    engine = "not-applicable"
    step_seed = None
    selection_reason = (
        "directional model does not use a stochastic ray flux engine"
        if directional
        else "native CSV rate-grid field does not use a stochastic ray flux engine"
        if csv_process
        else "native model does not use a stochastic ray flux engine"
        if specialized and not particle
        else "isotropic model does not use a ray flux engine"
    )
    if particle:
        engine = ctx.step_engines.get(step["id"])
        if engine is None:
            engine = simulate.requested_engine_for_step(ctx.project, step, ctx.step_engines)
        simulate.verify_worker_engine_evidence(ctx.project, step, engine)
        if engine not in {"CPU_DISK", "GPU_DISK", "CPU_TRIANGLE", "GPU_TRIANGLE"}:
            raise RuntimeError("no verified concrete flux engine for step")
        if process is not None:
            process.setFluxEngineType(getattr(ctx.ps.FluxEngineType, engine))
        selection_reason = "pinned per-step verified engine; no fallback"
        if ctx.run_seed is None:
            raise RuntimeError("a nonzero single-particle step requires an explicit run seed")
        step_seed = ctx.run_seed
    else:
        # Isotropic Process never consumes a ray flux engine.
        process.setFluxEngineType(ctx.ps.FluxEngineType.CPU_DISK)
        if geometric_isotropic:
            selection_reason = "CPU_DISK pinned; geometric SphereDistribution has no stochastic flux engine"
    advection, advection_actual = simulate.configure_advection(
        ctx.ps,
        ctx.project,
        time_step_ratio=(
            None if geometric_isotropic or isotropic_dispatch is None else step.get("isotropicTimeStepRatio")
        ),
        override=None if geometric_isotropic else step.get("advection"),
    )
    if isotropic_dispatch is not None:
        if geometric_isotropic:
            isotropic_dispatch["timeStepRatio"] = "ignored: geometric advection integrates no PDE"
        else:
            isotropic_dispatch["timeStepRatio"] = float(advection.timeStepRatio)
            isotropic_dispatch["timeStepRatioSource"] = advection_actual.get(
                "timeStepRatioSource", "solver.timeStepRatio"
            )
    if specialized and step["nativeModel"] == "oxideRegrowth":
        advection.ignoreVoids = True
        advection_actual["ignoreVoids"] = True
    if process is not None:
        process.setParameters(advection)
    if specialized:
        for process_parameters in special_process_parameters:
            process.setParameters(process_parameters)
    actual_parameters: dict[str, Any] = {
        **advection_actual,
        "duration": native_duration,
        "rate": rate,
    }
    if geometric_isotropic:
        # The native geometric strategy ignores the requested duration.
        actual_parameters["duration"] = 0.0
    if (
        ctx.project["schemaVersion"] in (4, 5, 6, 7)
        and not directional
        and not specialized
        and not csv_process
        and step.get("model") != "multiParticle"
    ):
        actual_parameters.update(
            {
                "rateMode": step["rateMode"],
                "actualDefaultRate": rate,
                "actualMaterialRatesByName": signed_material_rates,
                "actualMaterialRatesById": {
                    str(simulate.MATERIAL_IDS[name]): value for name, value in signed_material_rates.items()
                },
                "nativeRateTable": [
                    {"name": name, "id": simulate.MATERIAL_IDS[name], "rate": value}
                    for name, value in sorted(signed_material_rates.items())
                ],
            }
        )
        if material_mode:
            # Record both sides: the requested nonnegative table and
            # the signed values that reached the native process.
            actual_parameters["requestedDefaultRate"] = float(step.get("rateMagnitude", 0.0))
            actual_parameters["requestedMaterialRatesByName"] = {
                name: float(value) for name, value in (step.get("materialRates") or {}).items()
            }
            actual_parameters["operationSign"] = -1.0 if step["operation"] == "etch" else 1.0
    if isotropic_dispatch is not None:
        actual_parameters.update(isotropic_dispatch)
    if particle:
        ray_parameters, ray_actual = simulate.configure_rays(ctx.ps, ctx.project, int(step_seed))
        if process is not None:
            process.setParameters(ray_parameters)
        actual_parameters.update(ray_actual)
        if step["model"] == "singleParticle":
            actual_parameters.update(
                {
                    "stickingProbability": float(step["stickingProbability"]),
                    "sourceExponent": float(step["sourceExponent"]),
                }
            )
        elif step["model"] == "multiParticle":
            actual_parameters.update(
                {"multiParticle": multi_actual, "cpuThreads": int(ctx.project["solver"]["cpuThreads"])}
            )
            if gpu_multi_split:
                actual_parameters["multiParticleExecution"] = {
                    "strategy": simulate.GPU_MULTIPARTICLE_SPLIT_STRATEGY,
                    "sweeps": simulate.GPU_MULTIPARTICLE_SPLIT_SWEEPS,
                    "status": "scheduled; per-stage execution evidence follows after apply",
                }
            elif gpu_multi_ion:
                actual_parameters["multiParticleExecution"] = {
                    "strategy": simulate.GPU_MULTIPARTICLE_NATIVE_STRATEGY,
                    "nativeProcessCount": 1,
                    "nativeEngineFingerprint": ctx.native_engine_fingerprint,
                }
        elif specialized:
            actual_parameters.update(
                {"nativeModel": specialized_actual, "cpuThreads": int(ctx.project["solver"]["cpuThreads"])}
            )
    if directional:
        actual_parameters = {
            "timeStepRatio": float(advection.timeStepRatio),
            "duration": float(step["duration"]),
            **(directional_parameters or {}),
        }
        # A rate-set step has no single native rate; per-set values live
        # in actualParameters.rateSets and actualRate stays null.
        rate = (
            directional_parameters.get("nativeDirectionalVelocity")
            if directional_parameters and "nativeDirectionalVelocity" in directional_parameters
            else None
        )
    elif specialized:
        if specialized_actual is not None:
            specialized_actual.update(
                {
                    "actualSeed": step_seed,
                    "actualEngine": engine,
                    "cpuThreads": int(ctx.project["solver"]["cpuThreads"]),
                    "nativeProcessEngine": engine,
                }
            )
        actual_parameters.update({"nativeModel": specialized_actual, "processDurationSeconds": process_duration})
    elif csv_process:
        actual_parameters = {
            "timeStepRatio": float(advection.timeStepRatio),
            "duration": float(step["duration"]),
            "csvProcess": {
                **csv_process_actual,
                "actualSeed": step_seed,
                "actualEngine": engine,
                "cpuThreads": int(ctx.project["solver"]["cpuThreads"]),
                "processDurationSeconds": process_duration,
            },
        }
    ctx.event(
        {
            "type": "step-configured",
            "stepId": step["id"],
            "operation": step["operation"],
            "model": step["model"],
            "noOp": False,
            "actualEngine": engine,
            "engineSelectionReason": selection_reason,
            "requestedEnginePreference": ctx.engine_preference,
            "actualSeed": step_seed,
            "actualRate": rate,
            **(
                {
                    "isotropicMethod": isotropic_dispatch["actualIsotropicMethod"],
                    "nativeClass": isotropic_dispatch["nativeClass"],
                }
                if isotropic_dispatch is not None
                else {}
            ),
            "duration": native_duration,
            "actualParameters": actual_parameters,
        }
    )
    intermediate_dir = ctx.run_dir / "intermediate" / step["id"]
    intermediate_dir.mkdir(parents=True, exist_ok=True)
    split_result = None
    if gpu_multi_split:
        if ctx.selected_gpu_device is None:
            raise RuntimeError("GPU MultiParticle splitting requires the selected CUDA device UUID")
        split_result = simulate.apply_gpu_multi_particle_split(
            ctx.ps,
            ctx.domain,
            ctx.project,
            step,
            seed=int(step_seed),
            run_dir=ctx.run_dir,
            intermediate_dir=intermediate_dir,
            selected_gpu_device=ctx.selected_gpu_device,
        )
        process_apply_seconds = float(split_result["elapsedSeconds"])
        callback_state = split_result["callbackState"]
        actual_parameters["multiParticleExecution"] = split_result["execution"]
        ctx.event({"type": "multi-particle-gpu-split", "stepId": step["id"], "execution": split_result["execution"]})
    else:
        if process is None:
            raise RuntimeError("ViennaPS Process was not created for this process step")
        previous_cwd = Path.cwd()
        try:
            # ViennaLS 5.8.5 writes velocity VTPs relative to the process
            # working directory even when ViennaPS setIntermediateOutputPath
            # is set. Isolate each step inside its run artifact directory.
            os.chdir(intermediate_dir)
            process.setIntermediateOutputPath(str(intermediate_dir))
            process_started = time.perf_counter()
            process.apply()
            process_apply_seconds = time.perf_counter() - process_started
        finally:
            os.chdir(previous_cwd)
    if geometric_isotropic:
        geometric_after = simulate.surface_state(ctx.domain, int(ctx.project["dimension"]))
        assert isotropic_geometric_before is not None
        actual_parameters["surfaceSignatureBefore"] = isotropic_geometric_before["digest"]
        actual_parameters["surfaceSignatureAfter"] = geometric_after["digest"]
        actual_parameters["surfaceBoundsBeforeUm"] = isotropic_geometric_before["bounds"]
        actual_parameters["surfaceBoundsAfterUm"] = geometric_after["bounds"]
        if isotropic_geometric_before["digest"] == geometric_after["digest"]:
            masks = isotropic_dispatch.get("maskMaterials") if isotropic_dispatch else []
            raise RuntimeError(
                "geometric isotropic SphereDistribution left the surface unchanged; refusing "
                "to record a silent no-op (thickness must exceed the grid resolution"
                + (f", and the mask {masks} must not cover the whole exposed surface" if masks else "")
                + ")"
            )
    if shadow_domain is not None:
        _run_v2_plugin_shadow(ctx, step, shadow_domain, v2_registry)
    next_gpu_step_pending = any(
        simulate.uses_flux(future)
        and str(
            ctx.step_engines.get(future["id"], "GPU_DISK" if ctx.engine_preference == "gpu" else "CPU_DISK")
        ).startswith("GPU_")
        for future in ctx.actual_steps[index:]
    )
    if engine.startswith("GPU_"):
        if simulate.GPU_CONTEXT_REUSE and next_gpu_step_pending:
            # Must not touch the CUDA Runtime here: it invalidates the
            # live OptiX context and the next accel build fails (7001).
            post_apply_cuda_readback = {
                "readbackStatus": "skipped-for-context-reuse",
                "reason": "in-process CUDA Runtime calls between GPU steps break the next optixAccelBuild",
                "deviceEvidence": "nvidia-smi utilisation sample + pre-run CUDA Runtime readback",
            }
        else:
            post_apply_cuda_readback = simulate.cuda_runtime_readback(ctx.selected_gpu_device)
        gpu_usage = simulate.nvidia_process_readback(ctx.selected_gpu_device["uuid"])
        if ctx.gpu_sample is None:
            ctx.gpu_sample = simulate.nvidia_gpu_sample()
        step_cuda_readback = {
            **ctx.cuda_readback,
            "afterProcessApply": post_apply_cuda_readback,
            "process": gpu_usage,
            "gpuSample": ctx.gpu_sample,
            **({"stageSelection": split_result["gpuStageSelection"]} if split_result is not None else {}),
        }
        actual_parameters["cudaDeviceReadback"] = step_cuda_readback
        actual_parameters["processApplySeconds"] = process_apply_seconds
        ctx.event(
            {
                "type": "step-device-readback",
                "stepId": step["id"],
                "cudaDeviceReadback": step_cuda_readback,
                "processApplySeconds": process_apply_seconds,
            }
        )
    else:
        actual_parameters["processApplySeconds"] = process_apply_seconds
        ctx.event({"type": "step-performance", "stepId": step["id"], "processApplySeconds": process_apply_seconds})
    if callback_state is not None:
        if callback_state["count"] <= 0:
            raise RuntimeError("ViennaPS did not invoke the configured MultiParticleProcess rate function")
        callback_counts_by_species = callback_state.get("callbackCountBySpecies")
        if callback_counts_by_species:
            mean_flux_by_species = [
                float(total) / max(int(callback_counts_by_species[str(species["label"])]), 1)
                for total, species in zip(callback_state["fluxSums"], step["species"])
            ]
        else:
            mean_flux_by_species = [float(total) / callback_state["count"] for total in callback_state["fluxSums"]]
        ctx.event(
            {
                "type": "multi-particle-rate-callback",
                "stepId": step["id"],
                "invocationCount": callback_state["count"],
                "materials": callback_state["materials"],
                "samples": callback_state["samples"],
                "fluxSums": callback_state["fluxSums"],
                "fluxMaxima": callback_state["fluxMaxima"],
                "meanFluxBySpecies": mean_flux_by_species,
                **({"callbackCountBySpecies": callback_counts_by_species} if callback_counts_by_species else {}),
            }
        )
    for output_path in sorted(intermediate_dir.iterdir()):
        if not output_path.is_file():
            continue
        if output_path.suffix.lower() == ".vtp":
            output = simulate.describe_intermediate_vtp(
                output_path, step_id=step["id"], run_id=ctx.run_id, run_dir=ctx.run_dir, project=ctx.project
            )
        else:
            relative = output_path.resolve().relative_to(ctx.run_dir.resolve())
            output = {
                "stepId": step["id"],
                "fileName": relative.as_posix(),
                "url": f"/api/runs/{ctx.run_id}/intermediate-outputs/{step['id']}/{output_path.name}",
                "format": output_path.suffix.lstrip(".").upper() or "binary",
                "pointCount": None,
                "cellCounts": {},
                "coordinateUnit": None,
                "axisMap": None,
                "fields": [],
                "sizeBytes": output_path.stat().st_size,
                "sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
            }
        ctx.intermediate_outputs.append(output)
        ctx.event({"type": "intermediate-output", "output": output})
    native_specialized_outputs: list[dict[str, Any]] = []
    if specialized and step["nativeModel"] == "oxideRegrowth":
        cell_set = ctx.domain.getCellSet()
        if cell_set is None:
            raise RuntimeError("ViennaPS OxideRegrowth lost its generated DenseCellSet")
        cell_set_payload = {
            "schemaVersion": 1,
            "source": "viennaps.Domain.getCellSet after OxideRegrowth.Process.apply",
            "nativeType": "DenseCellSet",
            "dimension": int(ctx.project["dimension"]),
            "gridDelta": float(ctx.domain.getGridDelta()),
            "association": "DenseCellSet scalar grid; separate from the returned surface mesh",
            "derivedSetup": specialized_actual["derivedCellSet"],
            "numberOfCells": int(cell_set.getNumberOfCells()),
            "fields": {
                str(label): [float(value) for value in cell_set.getScalarData(str(label))]
                for label in cell_set.getScalarDataLabels()
            },
            "nativeUnit": None,
        }
        artifact_path = ctx.run_dir / "artifacts" / f"{step['id']}-dense-cell-set.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        simulate.atomic_json(artifact_path, cell_set_payload)
        native_specialized_outputs.append(
            simulate.output_artifact(
                artifact_path,
                ctx.run_dir,
                ctx.run_id,
                step["id"],
                "nativeCellSet",
                "JSON",
                association="native DenseCellSet scalar grid; not projected onto surface mesh",
                nativeUnit=None,
            )
        )
    snapshot_extra = None
    if (
        specialized
        and step["nativeModel"] == "oxidation"
        and isinstance(specialized_actual.get("advancedOxidation"), dict)
    ):
        # Keep historical snapshots byte-identical when the optional
        # block is omitted; only an explicit advanced object adds this
        # manifest section.
        snapshot_extra = {"oxidation": specialized_actual["advancedOxidation"]}
    with simulate.phase(f"stepSnapshot:{step['id']}"):
        metadata = ctx.save_snapshot(
            ctx.domain,
            step_id=step["id"],
            step_name=step["name"],
            step_index=index,
            requested=ctx.project,
            extra=snapshot_extra,
            extra_outputs=native_specialized_outputs or None,
            step=step,
            previous_tops=ctx.snapshots[-1].get("materialTops"),
            defer_payload=simulate.LAZY_SNAPSHOTS,
        )
    with simulate.phase(f"stepNativeState:{step['id']}"):
        metadata["nativeState"] = ctx.save_native_state(ctx.domain, step["id"])
    execution_record = {
        "stepId": step["id"],
        "engine": engine,
        "seed": step_seed,
        **({"gpuDeviceId": ctx.selected_gpu_device["uuid"]} if engine.startswith("GPU_") else {}),
    }
    if split_result is not None:
        execution_record.update(
            {
                "executionStrategy": split_result["execution"]["strategy"],
                "stageSeeds": split_result["execution"]["stageSeeds"],
            }
        )
    elif gpu_multi_ion:
        execution_record["executionStrategy"] = simulate.GPU_MULTIPARTICLE_NATIVE_STRATEGY
    ctx.actual_execution.append(execution_record)

    if os.environ.get("VIENNAPS_WORKBENCH_V2_EXECUTION") == "1":
        from worker.v2_state import WorkerV2State

        WorkerV2State.invalidate(ctx)
    return ProcessStepResult(metadata=metadata, process=process, model=model)
