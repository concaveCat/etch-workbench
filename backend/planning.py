"""Run planning for the single-run supervisor.

``plan_run`` is the validate/resolve/materialise half of ``Supervisor.start``:
it hashes the request, validates replay and continuation metadata, resolves
the engines, strategies and runtime, and finally creates the run directory
with ``input.json``, ``project.json``, ``run.log``, ``replay-project.json``
and ``manifest.json``.

It deliberately takes no lock. The supervisor holds ``self.lock`` across the
whole call so that the active-run gate and the run directory it creates
cannot interleave with a second request.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import materials as material_registry
from . import storage
from .engine import multi_ion_gpu_error
from .gpu_devices import cuda_devices, resolve_gpu_device
from .hashing import execution_hash, requested_input_hash
from .native_build import configured_runtime
from .rates import uses_flux
from .validation import (
    GPU_MULTIPARTICLE_FLUX_STRATEGY,
    GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY,
    GPU_MULTIPARTICLE_NATIVE_STRATEGY,
    GPU_MULTIPARTICLE_SPLIT_STRATEGY,
    capability_verified,
    experimental_cpu_replay_allowed,
    gpu_project_signature,
    select_engine,
    slice_position_issue,
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ReplayMismatch(ValueError):
    pass


@dataclass(frozen=True)
class RunPlan:
    """Everything ``Supervisor.start`` needs to hand a run to its thread."""

    run_id: str
    run_dir: Path
    kind: str
    request_hash: str
    run_seed: int | None
    actual_steps: list[dict[str, Any]]
    selected_gpu_device: dict[str, Any] | None
    native_engine_fingerprint: str | None
    input_config: dict[str, Any]
    manifest: dict[str, Any]
    replay_document: dict[str, Any]


def requested_input_hash_from_dict(project: dict[str, Any]) -> str:
    """Revalidate a normalized stored project before hashing it."""
    from .models import parse_project

    return requested_input_hash(parse_project(project))


def _requires_patched_multi_particle(step: dict[str, Any]) -> bool:
    """Only multi-ion GPU steps need the patched runtime.

    The installed 4.6.2 GPU binding silently ignores every ion
    after the first, so more than one ion is the one case where
    stock would produce a wrong result. Everything else stays on
    stock: a neutral with an empty material-sticking map fails
    loudly with CUDA_ERROR_UNKNOWN instead, which is preferable
    to injecting the patched build while its DirectionalProcess
    visibility path still segfaults.
    """
    species = step.get("species") or []
    return sum(1 for row in species if row.get("kind") == "ion") > 1


def plan_run(
    kind: str,
    project: dict[str, Any],
    *,
    replay: dict[str, Any] | None = None,
    analysis_step_id: str | None = None,
    slice_request: dict[str, Any] | None = None,
    inventory_provider: Callable[..., dict[str, Any]] = cuda_devices,
) -> RunPlan:
    """Validate one run request and materialise its run directory.

    Every refusal raised here happens before ``run_dir.mkdir`` so a rejected
    replay never leaves a run directory behind.
    """
    storage.check_disk_preflight()
    request_hash = requested_input_hash_from_dict(project)
    if replay and replay.get("analysisMode") == "flux":
        kind = "flux"
        analysis_step_id = replay.get("analysisStepId")
    elif replay and replay.get("analysisMode") == "slice":
        kind = "slice"
        slice_request = replay.get("sliceRequest")
    if kind == "slice":
        if project.get("schemaVersion") != 7 or project.get("dimension") != 3 or project.get("steps"):
            raise ReplayMismatch("native Slice requires a schema-7 3D geometry project without process steps")
        from .models import SliceRequestV7

        try:
            normalized_slice = SliceRequestV7.model_validate(slice_request).model_dump(mode="json", exclude_none=True)
        except ValueError as exc:
            raise ReplayMismatch(f"native Slice request is invalid: {exc}") from exc
        slice_request = normalized_slice
        if issue := slice_position_issue(project, slice_request):
            raise ReplayMismatch(issue)
        request_hash = hashlib.sha256(
            (request_hash + json.dumps(slice_request, sort_keys=True, separators=(",", ":"), allow_nan=False)).encode(
                "utf-8"
            )
        ).hexdigest()
    if kind == "simulate":
        enabled_steps = [step for step in project["steps"] if step["enabled"]]
    elif kind == "flux":
        selected = next((step for step in project["steps"] if step.get("id") == analysis_step_id), None)
        if project.get("schemaVersion") != 7 or not selected or not selected.get("enabled") or not uses_flux(selected):
            raise ReplayMismatch(
                "single flux analysis requires an enabled schema-7 particle-process step with a nonzero rate relation"
            )
        enabled_steps = [selected]
    else:
        enabled_steps = []
    uses_particle = any(uses_flux(step) for step in enabled_steps)
    if replay:
        expected_particle_ids = {step["id"] for step in enabled_steps if uses_flux(step)}
        if set(replay["stepsById"]) != expected_particle_ids:
            raise ReplayMismatch("replay requirements contain missing or unexpected particle steps")
        run_seed = replay["seed"] if uses_particle else None
    elif uses_particle:
        random_policy = project["solver"]["random"]
        run_seed = int(random_policy["seed"]) if random_policy["mode"] == "fixed" else secrets.randbelow(2**32)
    else:
        run_seed = None
    ps_version = importlib.metadata.version("viennaps")
    ls_version = importlib.metadata.version("viennals")
    expected_versions = {"viennaps": ps_version, "viennals": ls_version}
    state_ref = project.get("continuationState")
    state_path: Path | None = None
    if state_ref is not None:
        if state_ref.get("viennapsVersion") != ps_version or state_ref.get("viennalsVersion") != ls_version:
            raise ReplayMismatch(
                f"native Domain state requires ViennaPS {state_ref['viennapsVersion']} / "
                f"ViennaLS {state_ref['viennalsVersion']}; installed is {ps_version} / {ls_version}. "
                f"Saved state is never reinterpreted under a different runtime."
            )
        state_path = storage.resolve_domain_state(state_ref)
    if replay and (replay["viennapsVersion"] != ps_version or replay["viennalsVersion"] != ls_version):
        raise ReplayMismatch(
            f"replay requires ViennaPS {replay['viennapsVersion']} / "
            f"ViennaLS {replay['viennalsVersion']}; installed is {ps_version} / {ls_version}. "
            f"Old runs are never reinterpreted under a different runtime; start the matching runtime."
        )
    actual_steps = []
    analysis_mode = "flux" if kind == "flux" else "process"
    for step in enabled_steps:
        particle = uses_flux(step)
        engine = select_engine(project, step, analysis_mode=analysis_mode) if particle else "not-applicable"
        seed = run_seed if particle else None
        execution_strategy = None
        flux_strategy = None
        multi_ion_gpu = (
            project.get("schemaVersion") == 7
            and step.get("model") == "multiParticle"
            and sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1
            and engine == "GPU_DISK"
        )
        if multi_ion_gpu:
            if kind == "flux":
                flux_strategy = GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY
            else:
                execution_strategy = GPU_MULTIPARTICLE_NATIVE_STRATEGY
        if replay and particle:
            requirement = replay["stepsById"].get(step["id"])
            if not requirement:
                raise ReplayMismatch(f"replay requirements are missing step {step['id']}")
            engine = requirement["engine"]
            multi_ion_gpu = (
                project.get("schemaVersion") == 7
                and step.get("model") == "multiParticle"
                and sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1
                and engine == "GPU_DISK"
            )
            execution_strategy = requirement.get("executionStrategy")
            flux_strategy = requirement.get("fluxStrategy")
            valid_strategy = (
                execution_strategy in {GPU_MULTIPARTICLE_SPLIT_STRATEGY, GPU_MULTIPARTICLE_NATIVE_STRATEGY}
                and flux_strategy is None
                if multi_ion_gpu and kind != "flux"
                else flux_strategy in {GPU_MULTIPARTICLE_FLUX_STRATEGY, GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY}
                and execution_strategy is None
                if multi_ion_gpu and kind == "flux"
                else execution_strategy is None and flux_strategy is None
            )
            if not valid_strategy:
                raise ReplayMismatch(
                    "replay MultiParticle GPU strategy does not match its process or single-pass flux mode"
                )
            capability_model = (
                f"specialized:{step['nativeModel']}" if step.get("model") == "specialized" else step["model"]
            )
            boundary = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
            signature = gpu_project_signature(project, step)
            requested_gpu_engine = project.get("solver", {}).get("gpuEngine")
            if requested_gpu_engine is not None and engine != requested_gpu_engine:
                raise ReplayMismatch(
                    f"replay engine {engine} differs from the saved solver.gpuEngine {requested_gpu_engine}"
                )
            if engine == "GPU_TRIANGLE" and not capability_verified(
                project["dimension"],
                capability_model,
                step["operation"],
                boundary,
                engine,
                schema_version=project["schemaVersion"],
                execution_strategy=execution_strategy,
                flux_strategy=flux_strategy,
                **signature,
            ):
                raise ReplayMismatch(
                    f"GPU_TRIANGLE has no direct-reference acceptance row for {capability_model} "
                    f"{step['operation']} on {boundary} in {project['dimension']}D; replay is refused "
                    "instead of falling back to GPU_DISK or CPU_DISK"
                )
            if (
                not engine.startswith("GPU_")
                and not capability_verified(
                    project["dimension"],
                    capability_model,
                    step["operation"],
                    boundary,
                    engine,
                    schema_version=project["schemaVersion"],
                    execution_strategy=execution_strategy,
                    flux_strategy=flux_strategy,
                    **signature,
                )
                and not experimental_cpu_replay_allowed(step, engine)
            ):
                raise ReplayMismatch("unverified replay engine, geometry or material combination")
            if requirement["engine"] != engine or requirement["seed"] != run_seed:
                raise ReplayMismatch(f"replay engine or seed does not match step {step['id']}")
        actual_steps.append(
            {
                "stepId": step["id"],
                "engine": engine,
                "seed": seed,
                **({"executionStrategy": execution_strategy} if execution_strategy else {}),
                **({"fluxStrategy": flux_strategy} if flux_strategy else {}),
            }
        )
    selected_gpu_device = None
    if any(item["engine"].startswith("GPU_") for item in actual_steps):
        inventory = inventory_provider()
        try:
            selected_gpu_device = resolve_gpu_device(project.get("solver", {}).get("gpuDeviceId"), inventory)
        except RuntimeError as exc:
            # Automatic mode may use an independently verified CPU
            # engine when the host has no visible CUDA device. An
            # explicit device choice or explicit GPU preference never
            # silently changes device or engine.
            if (
                replay is None
                and project["solver"]["enginePreference"] == "auto"
                and project.get("solver", {}).get("gpuDeviceId") is None
            ):
                step_by_id = {step["id"]: step for step in enabled_steps}
                for item in actual_steps:
                    if not item["engine"].startswith("GPU_"):
                        continue
                    step = step_by_id[item["stepId"]]
                    model = (
                        f"specialized:{step['nativeModel']}" if step.get("model") == "specialized" else step["model"]
                    )
                    boundary = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
                    signature = gpu_project_signature(project, step)
                    if not capability_verified(
                        project["dimension"],
                        model,
                        step["operation"],
                        boundary,
                        "CPU_DISK",
                        schema_version=project.get("schemaVersion"),
                        **signature,
                    ):
                        raise ReplayMismatch(str(exc)) from exc
                    item["engine"] = "CPU_DISK"
                    item.pop("executionStrategy", None)
                    item.pop("fluxStrategy", None)
            else:
                raise ReplayMismatch(str(exc)) from exc
        if not any(item["engine"].startswith("GPU_") for item in actual_steps):
            selected_gpu_device = None
        elif selected_gpu_device is None:
            raise ReplayMismatch("GPU execution requires a concrete CUDA device; no implicit fallback is allowed")
        else:
            for item in actual_steps:
                if item["engine"].startswith("GPU_"):
                    item["gpuDeviceId"] = selected_gpu_device["uuid"]
    requested_fingerprint = replay.get("nativeEngineFingerprint") if replay else None
    native_strategy = any(
        item.get("executionStrategy") == GPU_MULTIPARTICLE_NATIVE_STRATEGY
        or item.get("fluxStrategy") == GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY
        for item in actual_steps
    )
    gpu_multi_steps = {step["id"]: step for step in enabled_steps if step.get("model") == "multiParticle"}
    gpu_multiparticle = any(item["stepId"] in gpu_multi_steps and item["engine"] == "GPU_DISK" for item in actual_steps)

    gpu_multiparticle_patch = any(
        item["stepId"] in gpu_multi_steps
        and item["engine"] == "GPU_DISK"
        and _requires_patched_multi_particle(gpu_multi_steps[item["stepId"]])
        for item in actual_steps
    )
    # Shared policy for every multi-ion GPU step, new run or replay,
    # process or single-pass flux, and independently of the recorded
    # strategy: a per-species flux replay needs no patched-runtime
    # fingerprint, so keying the gate off ``needs_native_patch`` left it
    # accepted as pending on 4.7.0 while the worker could only truncate
    # or guess. API validation, the worker and this supervisor agree.
    multi_ion_gpu_steps = [
        item["stepId"]
        for item in actual_steps
        if str(item["engine"]).startswith("GPU_")
        and item["stepId"] in gpu_multi_steps
        and _requires_patched_multi_particle(gpu_multi_steps[item["stepId"]])
    ]
    if multi_ion_gpu_steps:
        policy = multi_ion_gpu_error()
        if policy is not None:
            raise ReplayMismatch(policy)
    # Old replays without a fingerprint must retain their original
    # runtime and execution identity. New runs only inject the patched
    # build for the multi-particle shapes that actually need it.
    needs_native_patch = bool(requested_fingerprint) if replay else gpu_multiparticle_patch
    if replay and ((native_strategy and not needs_native_patch) or (needs_native_patch and not gpu_multiparticle)):
        raise ReplayMismatch("replay native engine identity does not match its GPU MultiParticle steps")
    native_engine_fingerprint = None
    if needs_native_patch:
        try:
            _, native_engine_fingerprint = configured_runtime(requested_fingerprint)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReplayMismatch(str(exc)) from exc
        if requested_fingerprint and requested_fingerprint != native_engine_fingerprint:
            raise ReplayMismatch("replay requires a different patched ViennaPS engine build")
    if replay and set(replay["stepsById"]) != {step["id"] for step in enabled_steps if uses_flux(step)}:
        raise ReplayMismatch("replay requirements contain missing or unexpected particle steps")
    run_id = secrets.token_hex(12)
    run_dir = storage.run_path(run_id)
    run_dir.mkdir(mode=0o700, parents=False)
    gds_asset_path = None
    geometry = project.get("geometry", {})
    gds_asset = (
        geometry
        if isinstance(geometry, dict) and geometry.get("kind") == "gds"
        else geometry.get("gdsMask")
        if isinstance(geometry, dict)
        else None
    )
    if isinstance(gds_asset, dict):
        source_gds = storage.resolve_gds_asset(gds_asset.get("assetId", ""))
        gds_asset_path = run_dir / "gds-source.gds"
        shutil.copyfile(source_gds, gds_asset_path)
        if storage.file_sha256(gds_asset_path) != gds_asset["assetId"]:
            raise OSError("copied GDS asset does not match its SHA-256 reference")
    csv_asset_paths: dict[str, Path] = {}
    for step in enabled_steps:
        if step.get("model") != "csvProcess":
            continue
        csv_asset_id = step.get("assetId")
        if not isinstance(csv_asset_id, str) or csv_asset_id in csv_asset_paths:
            continue
        source_csv = storage.resolve_csv_asset(csv_asset_id)
        copied_csv = run_dir / f"csv-source-{csv_asset_id}.csv"
        shutil.copyfile(source_csv, copied_csv)
        if storage.file_sha256(copied_csv) != csv_asset_id:
            raise OSError("copied CSV rate asset does not match its SHA-256 reference")
        csv_asset_paths[csv_asset_id] = copied_csv
    initial_state_path = None
    if state_path is not None:
        initial_state_path = run_dir / "domain-state.vpsd"
        shutil.copyfile(state_path, initial_state_path)
        if storage.file_sha256(initial_state_path) != state_ref["stateId"]:
            raise OSError("copied native Domain state does not match its SHA-256 reference")
    input_config = {
        "kind": kind,
        "runId": run_id,
        "project": project,
        "runSeed": run_seed,
        "expectedVersions": expected_versions,
        "stepEngines": {s["stepId"]: s["engine"] for s in actual_steps},
    }
    if native_engine_fingerprint is not None:
        input_config["nativeEngineFingerprint"] = native_engine_fingerprint
    input_config["stepExecutionStrategies"] = {
        item["stepId"]: item["executionStrategy"] for item in actual_steps if item.get("executionStrategy") is not None
    }
    input_config["stepFluxStrategies"] = {
        item["stepId"]: item["fluxStrategy"] for item in actual_steps if item.get("fluxStrategy") is not None
    }
    if selected_gpu_device is not None:
        input_config["selectedGpuDevice"] = selected_gpu_device
    custom_materials = project.get("materialDefinitions") or material_registry.worker_registry_payload()
    input_config["customMaterials"] = custom_materials
    if gds_asset_path is not None:
        input_config["gdsAssetPath"] = str(gds_asset_path)
    if csv_asset_paths:
        input_config["csvAssets"] = {asset_id: str(path) for asset_id, path in csv_asset_paths.items()}
    if kind == "flux":
        input_config["analysisStepId"] = analysis_step_id
    if kind == "slice":
        input_config["sliceRequest"] = slice_request
    if initial_state_path is not None:
        input_config["initialStatePath"] = str(initial_state_path)
    storage.atomic_json(run_dir / "input.json", input_config)
    storage.atomic_json(run_dir / "project.json", project)
    (run_dir / "run.log").touch(exist_ok=True)
    request_exec_hash = execution_hash(
        request_hash,
        run_seed=run_seed,
        cpu_threads=project["solver"]["cpuThreads"],
        step_engines=actual_steps,
        viennaps_version=ps_version,
        viennals_version=ls_version,
        native_engine_fingerprint=native_engine_fingerprint,
    )
    manifest = {
        "schemaVersion": project["schemaVersion"],
        "dimension": project["dimension"],
        "runId": run_id,
        "kind": kind,
        "status": "pending",
        "createdAt": now_iso(),
        "startedAt": None,
        "finishedAt": None,
        "elapsedSeconds": 0.0,
        "failureReason": None,
        "requestedInputHash": request_hash,
        "executionHash": None,
        "normalizedProject": project,
        "gdsAssetId": gds_asset.get("assetId") if isinstance(gds_asset, dict) else None,
        "csvAssetIds": sorted(csv_asset_paths) or None,
        "sliceRequest": slice_request if kind == "slice" else None,
        "requestedSolver": project["solver"],
        "selectedGpuDevice": selected_gpu_device,
        "materialRegistry": custom_materials,
        "outputSettings": project.get("outputSettings"),
        "actualSeed": run_seed,
        "cpuThreads": project["solver"]["cpuThreads"],
        "versions": {
            "application": "0.1.0",
            "python": sys.version.split()[0],
            "viennaps": ps_version,
            "viennals": ls_version,
        },
        **({"nativeEngineFingerprint": native_engine_fingerprint} if native_engine_fingerprint else {}),
        "plannedExecution": actual_steps,
        "actualExecution": [],
        "plannedExecutionHash": request_exec_hash,
        "supervisorProcess": None,
        "currentStepId": None,
        "completedStepIds": [],
        "snapshots": [],
        "noOpSteps": [],
        "replay": replay is not None,
        "runMode": "from-domain-state" if state_ref is not None else "from-initial",
        "initialState": {
            key: value for key, value in (state_ref or {}).items() if key != "sourceRunId" and key != "sourceStepId"
        }
        if state_ref is not None
        else None,
    }
    replay_project = json.loads(json.dumps(project, ensure_ascii=False))
    replay_actual_seed = run_seed
    if replay_actual_seed is None:
        random_config = project["solver"]["random"]
        seed_value = random_config.get("seed")
        replay_actual_seed = int(seed_value) if seed_value is not None else 0
    replay_project["solver"]["random"] = {"mode": "fixed", "seed": replay_actual_seed}
    # A preview does not execute its enabled steps, but its replay
    # document must still describe what would run. Resolve the same
    # concrete engine/strategy the simulate path would use.
    preview_requirements: list[dict[str, Any]] = []
    preview_native_strategy = False
    if kind == "preview":
        for step in project["steps"]:
            if not step.get("enabled") or not uses_flux(step):
                continue
            engine = select_engine(project, step)
            if engine not in {"CPU_DISK", "GPU_DISK", "CPU_TRIANGLE", "GPU_TRIANGLE"}:
                raise ReplayMismatch(f"preview replay cannot pin an engine for step {step['id']}: {engine}")
            execution_strategy = None
            if (
                project.get("schemaVersion") == 7
                and step.get("model") == "multiParticle"
                and sum(row.get("kind") == "ion" for row in step.get("species", [])) > 1
                and engine == "GPU_DISK"
            ):
                execution_strategy = GPU_MULTIPARTICLE_NATIVE_STRATEGY
                preview_native_strategy = True
            preview_requirements.append(
                {
                    "stepId": step["id"],
                    "engine": engine,
                    "seed": replay_actual_seed,
                    **({"executionStrategy": execution_strategy} if execution_strategy else {}),
                }
            )
    replay_items = actual_steps if actual_steps else preview_requirements
    replay_engine = next(
        (
            item["engine"]
            for item in replay_items
            if item["engine"] in {"CPU_DISK", "GPU_DISK", "CPU_TRIANGLE", "GPU_TRIANGLE"}
        ),
        None,
    )
    if replay_engine is not None:
        replay_project["solver"]["enginePreference"] = "gpu" if replay_engine.startswith("GPU_") else "cpu"
    elif replay_project["solver"].get("gpuEngine") is not None:
        # A preview has no executed particle step, so there is no
        # actual engine to pin. Keep the requested gpu/gpuEngine pair
        # intact; forcing "cpu" here would leave an invalid
        # combination that the replay schema rejects.
        replay_project["solver"]["enginePreference"] = "gpu"
    else:
        replay_project["solver"]["enginePreference"] = "cpu"
    if selected_gpu_device is not None and project["schemaVersion"] == 7:
        replay_project["solver"]["gpuDeviceId"] = selected_gpu_device["uuid"]
    replay_requirements = [
        {
            "stepId": item["stepId"],
            "engine": item["engine"],
            "seed": replay_actual_seed,
            **({"executionStrategy": item["executionStrategy"]} if item.get("executionStrategy") else {}),
            **({"fluxStrategy": item["fluxStrategy"]} if item.get("fluxStrategy") else {}),
        }
        for item in replay_items
        if item["engine"] in {"CPU_DISK", "GPU_DISK", "CPU_TRIANGLE", "GPU_TRIANGLE"}
    ]
    preview_native_fingerprint = None
    if preview_native_strategy:
        try:
            _, preview_native_fingerprint = configured_runtime(None)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ReplayMismatch(str(exc)) from exc
    replay_native_fingerprint = native_engine_fingerprint or preview_native_fingerprint
    replay_document = {
        "format": "viennaps-replay",
        "schemaVersion": project["schemaVersion"],
        "normalizedProject": replay_project,
        "executionRequirements": {
            "viennapsVersion": ps_version,
            "viennalsVersion": ls_version,
            "cpuThreads": project["solver"]["cpuThreads"],
            "steps": replay_requirements,
            **({"nativeEngineFingerprint": replay_native_fingerprint} if replay_native_fingerprint else {}),
        },
    }
    if project["schemaVersion"] == 6:
        replay_document["replayMode"] = "from-domain-state" if state_ref is not None else "from-initial"
    elif project["schemaVersion"] == 7:
        replay_document.update(
            {
                "replayMode": "from-domain-state" if state_ref is not None else "from-initial",
                "analysisMode": "flux" if kind == "flux" else "slice" if kind == "slice" else "process",
            }
        )
        if kind == "flux":
            replay_document["analysisStepId"] = analysis_step_id
        if kind == "slice":
            replay_document["sliceRequest"] = slice_request
    storage.atomic_json(run_dir / "replay-project.json", replay_document)
    storage.atomic_json(run_dir / "manifest.json", manifest)
    return RunPlan(
        run_id=run_id,
        run_dir=run_dir,
        kind=kind,
        request_hash=request_hash,
        run_seed=run_seed,
        actual_steps=actual_steps,
        selected_gpu_device=selected_gpu_device,
        native_engine_fingerprint=native_engine_fingerprint,
        input_config=input_config,
        manifest=manifest,
        replay_document=replay_document,
    )
