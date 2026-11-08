"""Run lifecycle, result inspection and download routes.

The handlers keep the exact request/response shapes of the former ``main.py``
routes. ``tail_router`` holds the two routes that were registered after the
continuation-package routes in ``main.py``; including it separately preserves
the application's route table order byte for byte.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from .. import preflight as artifact_preflight
from .. import storage
from ..api_errors import project_or_422 as _project_or_422
from ..api_errors import validation_error_response as _validation_error
from ..engine import EngineVersionMismatch, multi_ion_gpu_error
from ..models import (
    FluxRunRequest,
    RunRequest,
    SliceRequestV7,
    normalized_geometry_v3,
    parse_project,
    parse_replay,
    project_structure_hash,
)
from ..rates import uses_flux
from ..runtime_limits import artifact_limit_bytes, artifact_limit_label
from ..storage import read_json
from ..streamzip import stream_zip_async
from ..supervisor import ActiveRunError, ReplayMismatch
from ..validation import (
    GPU_MULTIPARTICLE_FLUX_STRATEGY,
    GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY,
    GPU_MULTIPARTICLE_NATIVE_STRATEGY,
    GPU_MULTIPARTICLE_SPLIT_STRATEGY,
    capability_verified,
    experimental_cpu_replay_allowed,
    gpu_project_signature,
)
from .deps import supervisor as _supervisor
from .deps import version as _version

router = APIRouter()
tail_router = APIRouter()


@router.post("/api/runs", status_code=202)
async def run_create(request: Request, payload: Any = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"kind", "project"}:
        raise HTTPException(
            status_code=422, detail=[{"path": "$", "message": "body must contain exactly kind and project"}]
        )
    try:
        run_request = RunRequest.model_validate(payload)
    except ValidationError as exc:
        return _validation_error(exc)
    try:
        project, normalized, _warnings = _project_or_422(
            run_request.project.model_dump(mode="python", exclude_none=True), kind=run_request.kind
        )
    except HTTPException:
        raise
    estimate = artifact_preflight.estimate_artifacts(normalized, kind=run_request.kind)
    if artifact_preflight.should_reject(estimate):
        raise HTTPException(status_code=422, detail=artifact_preflight.rejection_detail(estimate))
    try:
        result = _supervisor(request).start(run_request.kind, normalized)
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "runId": exc.run_id}) from exc
    except ReplayMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    # Additive fields: older clients ignore them; the run response keeps its
    # runId/status/requestedInputHash contract.
    warning = artifact_preflight.warning_message(estimate)
    if warning:
        result["artifactWarning"] = warning
    result["artifactEstimate"] = estimate
    return result


@router.post("/api/runs/flux", status_code=202)
async def flux_run_create(request: Request, payload: Any = Body(...)) -> dict[str, Any]:
    try:
        flux_request = FluxRunRequest.model_validate(payload)
    except ValidationError as exc:
        return _validation_error(exc)
    try:
        _parsed, normalized, _warnings = _project_or_422(
            flux_request.project.model_dump(mode="python", exclude_none=True)
        )
    except HTTPException:
        raise
    step = next((item for item in normalized["steps"] if item["id"] == flux_request.stepId), None)
    if not step or not step.get("enabled") or not uses_flux(step):
        raise HTTPException(
            status_code=422,
            detail={
                "path": "stepId",
                "message": "select an enabled particle-process step with a nonzero rate relation",
            },
        )
    try:
        return _supervisor(request).start("flux", normalized, analysis_step_id=flux_request.stepId)
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "runId": exc.run_id}) from exc
    except ReplayMismatch as exc:
        raise HTTPException(status_code=422, detail={"path": "stepId", "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@router.post("/api/runs/slice", status_code=202)
async def slice_run_create(request: Request, payload: Any = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"project", "sliceRequest"}:
        raise HTTPException(status_code=422, detail="body must contain exactly project and sliceRequest")
    try:
        slice_request = SliceRequestV7.model_validate(payload["sliceRequest"])
        parsed_project = parse_project(payload["project"])
    except ValidationError as exc:
        return _validation_error(exc)
    if parsed_project.schemaVersion != 7 or parsed_project.dimension != 3:
        requested_axis = slice_request.sliceAxis or "y"
        reason = (
            f"native Slice requires a schema-7 3D geometry project; {requested_axis}-normal slicing "
            "is not applicable to a 2D project"
        )
        raise HTTPException(status_code=422, detail={"path": "project", "message": reason})
    project_payload = parsed_project.model_dump(mode="python", exclude_none=True)
    project_payload["steps"] = []
    try:
        _project, normalized, _warnings = _project_or_422(project_payload)
        return _supervisor(request).start(
            "slice", normalized, slice_request=slice_request.model_dump(mode="json", exclude_none=True)
        )
    except HTTPException:
        raise
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "runId": exc.run_id}) from exc
    except ReplayMismatch as exc:
        raise HTTPException(status_code=422, detail={"path": "sliceRequest", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="slice project references an unavailable native Domain state or GDS asset"
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@router.post("/api/runs/replay", status_code=202)
async def run_replay(request: Request, payload: Any = Body(...)) -> dict[str, Any]:
    try:
        envelope = parse_replay(payload)
    except ValidationError as exc:
        return _validation_error(exc)
    project_payload = envelope.normalizedProject.model_dump(mode="python", exclude_none=True)
    analysis_mode = getattr(envelope, "analysisMode", "process")
    analysis_step_id = getattr(envelope, "analysisStepId", None)
    has_enabled = any(step["enabled"] for step in project_payload["steps"])
    kind = (
        "flux"
        if analysis_mode == "flux"
        else "slice"
        if analysis_mode == "slice"
        else "simulate"
        if has_enabled
        else "preview"
    )
    project, normalized, _warnings = _project_or_422(project_payload, kind="simulate" if kind == "flux" else kind)
    if envelope.schemaVersion != project.schemaVersion:
        raise HTTPException(
            status_code=422,
            detail={"path": "schemaVersion", "message": "replay envelope and normalized project versions must match"},
        )
    requirements = envelope.executionRequirements
    installed_ps = _version("viennaps")
    installed_ls = _version("viennals")
    if requirements.viennapsVersion != installed_ps or requirements.viennalsVersion != installed_ls:
        raise HTTPException(
            status_code=422,
            detail={
                "path": "executionRequirements",
                "message": (
                    f"requires ViennaPS {requirements.viennapsVersion} / ViennaLS {requirements.viennalsVersion}; "
                    f"installed is {installed_ps} / {installed_ls}. Old runs are never reinterpreted under a "
                    f"different runtime: start the matching ViennaPS {requirements.viennapsVersion} runtime to replay this run."
                ),
                "requiredVersions": {
                    "viennaps": requirements.viennapsVersion,
                    "viennals": requirements.viennalsVersion,
                },
                "installedVersions": {"viennaps": installed_ps, "viennals": installed_ls},
            },
        )
    if requirements.cpuThreads != project_payload["solver"]["cpuThreads"]:
        raise HTTPException(
            status_code=422,
            detail={
                "path": "executionRequirements.cpuThreads",
                "message": "thread count does not match the normalized project",
            },
        )
    if project_payload["solver"]["random"]["mode"] != "fixed":
        raise HTTPException(
            status_code=422,
            detail={
                "path": "normalizedProject.solver.random",
                "message": "replay project must store the actual fixed seed",
            },
        )
    if analysis_mode == "flux":
        selected_flux = next((step for step in project_payload["steps"] if step["id"] == analysis_step_id), None)
        if not selected_flux or not selected_flux.get("enabled") or not uses_flux(selected_flux):
            raise HTTPException(
                status_code=422,
                detail={
                    "path": "analysisStepId",
                    "message": "flux replay requires an enabled step whose installed ViennaPS model calculates particle flux",
                },
            )
        particle_ids = {analysis_step_id}
    else:
        particle_ids = {step["id"] for step in project_payload["steps"] if step["enabled"] and uses_flux(step)}
    requirements_by_id: dict[str, dict[str, Any]] = {}
    seeds = set()
    for item in requirements.steps:
        allowed_engines = (
            {"CPU_DISK", "GPU_DISK"}
            if envelope.schemaVersion == 1
            else {"CPU_DISK", "CPU_TRIANGLE", "GPU_DISK", "GPU_TRIANGLE"}
        )
        if item.engine not in allowed_engines:
            raise HTTPException(
                status_code=422,
                detail={
                    "path": "executionRequirements.steps",
                    "message": "particle replay engine is not enabled for this schema and dimension",
                },
            )
        if envelope.schemaVersion >= 3:
            requests_gpu = project_payload["solver"]["enginePreference"] == "gpu"
            requires_gpu = item.engine.startswith("GPU_")
            if requests_gpu != requires_gpu:
                expected = "GPU" if requests_gpu else "CPU"
                raise HTTPException(
                    status_code=422,
                    detail={
                        "path": "executionRequirements.steps",
                        "message": f"replay engine must match normalizedProject.solver.enginePreference ({expected})",
                    },
                )
            requested_gpu_engine = project_payload["solver"].get("gpuEngine")
            if requested_gpu_engine is not None and item.engine != requested_gpu_engine:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "path": "executionRequirements.steps",
                        "message": (
                            f"replay engine {item.engine} differs from normalizedProject.solver.gpuEngine "
                            f"{requested_gpu_engine}"
                        ),
                    },
                )
        if envelope.schemaVersion in (2, 3, 4, 5, 6, 7):
            step = next((s for s in project_payload["steps"] if s["id"] == item.stepId), None)
            replay_dimension = project_payload["dimension"]
            boundary = project_payload["domain"].get("boundaryXY", project_payload["domain"].get("xBoundary"))
            capability_model = (
                f"specialized:{step['nativeModel']}"
                if step and step.get("model") == "specialized"
                else step["model"]
                if step
                else ""
            )
            signature = gpu_project_signature(project_payload, step) if step else {}
            ion_count = sum(row.get("kind") == "ion" for row in step.get("species", [])) if step else 0
            multi_ion_gpu = (
                project_payload["schemaVersion"] == 7
                and item.engine == "GPU_DISK"
                and capability_model == "multiParticle"
                and ion_count > 1
            )
            if (
                project_payload["schemaVersion"] == 7
                and str(item.engine).startswith("GPU_")
                and capability_model == "multiParticle"
                and ion_count > 1
            ):
                # Same shared policy the worker and supervisor apply, so the API
                # never queues a replay that the worker would have to truncate.
                policy = multi_ion_gpu_error()
                if policy is not None:
                    raise HTTPException(
                        status_code=422, detail={"path": "executionRequirements.steps", "message": policy}
                    )
            valid_strategy = (
                item.executionStrategy in {GPU_MULTIPARTICLE_SPLIT_STRATEGY, GPU_MULTIPARTICLE_NATIVE_STRATEGY}
                and item.fluxStrategy is None
                if multi_ion_gpu and analysis_mode != "flux"
                else item.fluxStrategy in {GPU_MULTIPARTICLE_FLUX_STRATEGY, GPU_MULTIPARTICLE_NATIVE_FLUX_STRATEGY}
                and item.executionStrategy is None
                if multi_ion_gpu and analysis_mode == "flux"
                else item.executionStrategy is None and item.fluxStrategy is None
            )
            if not valid_strategy:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "path": "executionRequirements.steps",
                        "message": "GPU MultiParticle replay must record the strategy for its process or single-pass flux mode",
                    },
                )
            if (
                valid_strategy
                and item.engine == "GPU_TRIANGLE"
                and (
                    not step
                    or not capability_verified(
                        replay_dimension,
                        capability_model,
                        step["operation"],
                        boundary,
                        item.engine,
                        schema_version=envelope.schemaVersion,
                        execution_strategy=item.executionStrategy,
                        flux_strategy=item.fluxStrategy,
                        **signature,
                    )
                )
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "path": "executionRequirements.steps",
                        "message": (
                            f"GPU_TRIANGLE has no direct-reference acceptance row for {capability_model} "
                            f"{step['operation'] if step else ''} on {boundary} in {replay_dimension}D; "
                            "replay is refused instead of falling back to GPU_DISK or CPU_DISK"
                        ),
                    },
                )
            if not step or (
                not item.engine.startswith("GPU_")
                and not capability_verified(
                    replay_dimension,
                    capability_model,
                    step["operation"],
                    boundary,
                    item.engine,
                    schema_version=envelope.schemaVersion,
                    execution_strategy=item.executionStrategy,
                    flux_strategy=item.fluxStrategy,
                    **signature,
                )
                and not experimental_cpu_replay_allowed(step, item.engine)
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "path": "executionRequirements.steps",
                        "message": "unverified replay engine, geometry or material combination",
                    },
                )
        if item.stepId in requirements_by_id:
            raise HTTPException(
                status_code=422, detail={"path": "executionRequirements.steps", "message": "duplicate stepId"}
            )
        requirements_by_id[item.stepId] = item.model_dump(mode="json", exclude_none=True)
        seeds.add(item.seed)
    if set(requirements_by_id) != particle_ids:
        raise HTTPException(
            status_code=422,
            detail={
                "path": "executionRequirements.steps",
                "message": "requirements must contain exactly the enabled singleParticle steps",
            },
        )
    replay_engines = {item["engine"] for item in requirements_by_id.values()}
    if envelope.schemaVersion == 1 and len(replay_engines) > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "path": "executionRequirements.steps",
                "message": "v1 requires one concrete engine for all particle steps",
            },
        )
    if len(seeds) > 1:
        raise HTTPException(
            status_code=422,
            detail={
                "path": "executionRequirements.steps",
                "message": "v1 requires the same explicit seed for every particle step",
            },
        )
    if particle_ids:
        replay_seed = next(iter(seeds))
        if replay_seed != project_payload["solver"]["random"]["seed"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "path": "normalizedProject.solver.random.seed",
                    "message": "seed does not match the step requirements",
                },
            )
        replay_engine = next(iter(replay_engines))
        expected_preference = "gpu" if replay_engine.startswith("GPU_") else "cpu"
        if envelope.schemaVersion == 1 and project_payload["solver"]["enginePreference"] != expected_preference:
            raise HTTPException(
                status_code=422,
                detail={
                    "path": "normalizedProject.solver.enginePreference",
                    "message": f"replay project must explicitly request {expected_preference}",
                },
            )
    elif requirements.steps:
        raise HTTPException(
            status_code=422, detail={"path": "executionRequirements.steps", "message": "no particle steps are enabled"}
        )
    replay_info = {
        "viennapsVersion": requirements.viennapsVersion,
        "viennalsVersion": requirements.viennalsVersion,
        "stepsById": requirements_by_id,
        "seed": next(iter(seeds), project_payload["solver"]["random"]["seed"]),
    }
    if requirements.nativeEngineFingerprint is not None:
        replay_info["nativeEngineFingerprint"] = requirements.nativeEngineFingerprint
    if envelope.schemaVersion in (6, 7):
        replay_info["replayMode"] = envelope.replayMode
    if envelope.schemaVersion == 7:
        replay_info.update({"analysisMode": analysis_mode, "analysisStepId": analysis_step_id})
        if analysis_mode == "slice":
            replay_info["sliceRequest"] = envelope.sliceRequest.model_dump(mode="json")
    try:
        return _supervisor(request).start(kind, normalized, replay=replay_info)
    except ActiveRunError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "runId": exc.run_id}) from exc
    except ReplayMismatch as exc:
        raise HTTPException(status_code=422, detail={"path": "executionRequirements", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="replay requires a native Domain state that is not available in this workbench"
        ) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@router.get("/api/runs")
async def runs_list() -> dict[str, Any]:
    runs = []
    for path in storage.RUNS.iterdir():
        if not path.is_dir():
            continue
        try:
            item = read_json(path / "manifest.json")
            runs.append(
                {
                    "runId": item["runId"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "createdAt": item.get("createdAt"),
                    "finishedAt": item.get("finishedAt"),
                    "requestedInputHash": item.get("requestedInputHash"),
                    "failureReason": item.get("failureReason"),
                }
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {"runs": sorted(runs, key=lambda item: item.get("createdAt") or "", reverse=True)}


@router.get("/api/runs/{run_id}")
async def run_get(run_id: str, request: Request, cursor: int = Query(0, ge=0)) -> dict[str, Any]:
    try:
        return _supervisor(request).get_run(run_id, cursor=cursor)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return _supervisor(request).cancel(run_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/api/runs/{run_id}/snapshots/{step_id}")
async def snapshot_get(run_id: str, step_id: str) -> dict[str, Any]:
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if not any(item.get("stepId") == step_id for item in manifest.get("snapshots", [])):
            raise FileNotFoundError("snapshot is not listed in the manifest")
        filename = f"{step_id}.json"
        if Path(filename).name != filename:
            raise FileNotFoundError("invalid snapshot name")
        if not (run_dir / filename).is_file():
            from .. import snapshots as snapshot_store

            snapshot_store.ensure_materialized(run_dir, step_id)
        return read_json(run_dir / filename)
    except EngineVersionMismatch as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="snapshot not found") from exc


@router.get("/api/runs/{run_id}/flux-result")
async def flux_result_get(run_id: str) -> dict[str, Any]:
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        reference = manifest.get("fluxResult")
        if not isinstance(reference, dict) or reference.get("fileName") != "flux.json":
            raise FileNotFoundError("run has no stored native flux result")
        return read_json(run_dir / "flux.json")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="native flux result not found") from exc


@router.get("/api/runs/{run_id}/intermediate-outputs/{step_id}/{file_name}")
async def intermediate_output_get(run_id: str, step_id: str, file_name: str) -> FileResponse:
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if not step_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in step_id
        ):
            raise FileNotFoundError("invalid intermediate step ID")
        if Path(file_name).name != file_name or file_name in {".", ".."} or "\\" in file_name:
            raise FileNotFoundError("invalid intermediate output name")
        expected_name = f"intermediate/{step_id}/{file_name}"
        reference = next(
            (
                item
                for item in manifest.get("intermediateOutputs", [])
                if isinstance(item, dict) and item.get("stepId") == step_id and item.get("fileName") == expected_name
            ),
            None,
        )
        if reference is None:
            raise FileNotFoundError("intermediate output is not listed in the run manifest")
        path = run_dir / expected_name
        resolved = path.resolve(strict=True)
        resolved.relative_to(run_dir.resolve())
        if path.is_symlink() or not resolved.is_file() or resolved.stat().st_size > 128 * 1024 * 1024:
            raise FileNotFoundError("intermediate output is invalid or exceeds the download limit")
        if storage.file_sha256(resolved) != reference.get("sha256"):
            raise FileNotFoundError("intermediate output hash does not match the run manifest")
        media_type = "application/vnd.vtk+xml" if reference.get("format") == "VTP" else "application/octet-stream"
        return FileResponse(resolved, media_type=media_type, filename=file_name)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="native intermediate output not found") from exc


@router.get("/api/runs/{run_id}/artifacts/{step_id}")
async def native_artifact_download(
    run_id: str, step_id: str, fileName: str = Query(min_length=1, max_length=512)
) -> FileResponse:
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        snapshot = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == step_id), None)
        artifact = next(
            (item for item in (snapshot or {}).get("outputs", []) if item.get("fileName") == fileName), None
        )
        if artifact is None:
            raise FileNotFoundError("artifact is not listed in the selected snapshot")
        relative = Path(fileName)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or "\\" in fileName:
            raise FileNotFoundError("artifact path is invalid")
        path = run_dir / relative
        resolved = path.resolve(strict=True)
        resolved.relative_to(run_dir.resolve())
        if path.is_symlink() or resolved.stat().st_size != artifact.get("sizeBytes"):
            raise FileNotFoundError("artifact is missing or its size changed")
        if storage.file_sha256(resolved) != artifact.get("sha256"):
            raise FileNotFoundError("artifact SHA-256 does not match the run manifest")
        media_type = (
            "application/json"
            if artifact.get("format") == "JSON"
            else "application/vnd.vtk+xml"
            if artifact.get("format") in {"VTP", "VTU"}
            else "application/octet-stream"
        )
        return FileResponse(
            resolved, media_type=media_type, filename=resolved.name, headers={"Cache-Control": "no-store"}
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=404, detail="native output artifact not found or failed integrity verification"
        ) from exc


@router.get("/api/runs/{run_id}/continuation-project/{step_id}")
async def continuation_project_get(run_id: str, step_id: str) -> dict[str, Any]:
    """Create an empty schema-6 recipe whose initial Domain is a saved native state."""
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") not in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
            raise HTTPException(
                status_code=409, detail="continuation is available after the run reaches a terminal state"
            )
        row = next((item for item in manifest.get("snapshots", []) if item.get("stepId") == step_id), None)
        state = row.get("nativeState") if row else None
        if not isinstance(state, dict):
            raise HTTPException(status_code=409, detail="this historical snapshot has no saved native Domain state")
        filename = state.get("fileName")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".vpsd"):
            raise HTTPException(status_code=409, detail="historical native Domain state reference is invalid")
        source = run_dir / filename
        if not source.is_file() or storage.file_sha256(source) != state.get("stateId"):
            raise HTTPException(
                status_code=409, detail="historical native Domain state is missing or has a SHA-256 mismatch"
            )
        if state.get("viennapsVersion") != _version("viennaps") or state.get("viennalsVersion") != _version("viennals"):
            raise HTTPException(
                status_code=409,
                detail="historical native Domain state engine versions do not match the installed ViennaPS/ViennaLS",
            )
        original = parse_project(manifest["normalizedProject"])
        geometry = normalized_geometry_v3(original)
        expected_structure = project_structure_hash(original.dimension, original.units, original.domain, geometry)
        if state.get("structureHash") != expected_structure:
            raise HTTPException(
                status_code=409, detail="saved native state structure fingerprint does not match its source project"
            )
        storage.register_domain_state(source, state["stateId"])
        reference = {
            key: value for key, value in state.items() if key not in {"fileName", "sourceRunId", "sourceStepId"}
        }
        solver = original.solver.model_dump(mode="json", exclude_none=True)
        solver["enginePreference"] = "cpu"
        result = {
            "schemaVersion": 7 if original.schemaVersion == 7 else 6,
            "name": f"{original.name[:72]} · 新工艺" if len(original.name) <= 72 else f"{original.name[:72]}新工艺",
            "dimension": original.dimension,
            "units": original.units.model_dump(mode="json"),
            "domain": original.domain.model_dump(mode="json", exclude_none=True),
            "geometry": geometry,
            "steps": [],
            "solver": solver,
            "continuationState": reference,
        }
        if original.dimension == 2:
            result["view"] = original.view.model_dump(mode="json")
        else:
            result["view3D"] = original.view3D.model_dump(mode="json")
        _project, normalized, warnings = _project_or_422(result)
        return {
            "project": normalized,
            "continuation": {
                "sourceRunId": run_id,
                "sourceStepId": step_id,
                "engineVersions": {"viennaps": state["viennapsVersion"], "viennals": state["viennalsVersion"]},
            },
            "warnings": warnings,
        }
    except HTTPException:
        raise
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="saved continuation state not found") from exc


@tail_router.get("/api/runs/{run_id}/replay-project")
async def replay_project_get(run_id: str) -> dict[str, Any]:
    try:
        return read_json(storage.run_path(run_id) / "replay-project.json")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="replay project not found") from exc


def _archive_entries(run_dir: Path, manifest: dict[str, Any]) -> list[tuple[str, Path]]:
    candidates = [
        (name, run_dir / name) for name in ("project.json", "replay-project.json", "manifest.json", "run.log")
    ]
    gds_asset_id = manifest.get("gdsAssetId")
    if isinstance(gds_asset_id, str) and len(gds_asset_id) == 64 and (run_dir / "gds-source.gds").is_file():
        candidates.append((f"assets/{gds_asset_id}.gds", run_dir / "gds-source.gds"))
        candidates.extend((name, run_dir / name) for name in ("gds-selected.gds", "gds-transform.json"))
    for csv_asset_id in manifest.get("csvAssetIds") or []:
        source = run_dir / f"csv-source-{csv_asset_id}.csv"
        if isinstance(csv_asset_id, str) and len(csv_asset_id) == 64 and source.is_file():
            candidates.append((f"assets/{csv_asset_id}.csv", source))
    if (run_dir / "domain-state.vpsd").is_file():
        candidates.append(("domain-state.vpsd", run_dir / "domain-state.vpsd"))
    v2_root = run_dir / "v2"
    if v2_root.is_dir():
        for path in sorted(v2_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name not in {"plan.json", "outputs.json", "state.json"}:
                continue
            candidates.append((path.relative_to(run_dir).as_posix(), path))
    flux_result = manifest.get("fluxResult")
    if isinstance(flux_result, dict):
        for key in ("fileName", "vtk"):
            name = flux_result.get(key)
            if isinstance(name, str) and Path(name).name == name and name in {"flux.json", "flux.vtp"}:
                candidates.append((name, run_dir / name))
    for output in manifest.get("intermediateOutputs", []):
        name = output.get("fileName") if isinstance(output, dict) else None
        if not isinstance(name, str) or "\\" in name:
            continue
        relative = Path(name)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            continue
        path = run_dir / relative
        try:
            path.resolve().relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        candidates.append((relative.as_posix(), path))
    for item in manifest.get("snapshots", []):
        for key in ("snapshot", "vtk"):
            name = item.get(key)
            if not isinstance(name, str) or Path(name).name != name:
                continue
            candidates.append((name, run_dir / name))
        for output in item.get("outputs", []):
            name = output.get("fileName") if isinstance(output, dict) else None
            if not isinstance(name, str) or "\\" in name:
                continue
            relative = Path(name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                continue
            path = run_dir / relative
            try:
                path.resolve().relative_to(run_dir.resolve())
            except (OSError, ValueError):
                continue
            if not path.is_symlink() and path.is_file() and storage.file_sha256(path) == output.get("sha256"):
                candidates.append((relative.as_posix(), path))
        native_state = item.get("nativeState")
        if isinstance(native_state, dict):
            name = native_state.get("fileName")
            if isinstance(name, str) and Path(name).name == name and name.endswith(".vpsd"):
                candidates.append((name, run_dir / name))
    unique: dict[str, Path] = {}
    total = 0
    budget = 22
    for name, path in candidates:
        if name in unique or not path.is_file():
            continue
        size = path.stat().st_size
        encoded_length = len(name.encode("utf-8"))
        total += size
        budget += size + (size >> 12) + (size >> 14) + (size >> 25) + 13 + 92 + encoded_length * 2
        unique[name] = path
    archive_limit = artifact_limit_bytes()
    if total > archive_limit or budget > archive_limit:
        raise HTTPException(
            status_code=413, detail=f"run result package exceeds the {artifact_limit_label()} archive limit"
        )
    return list(unique.items())


@tail_router.get("/api/runs/{run_id}/download")
async def run_download(run_id: str):
    try:
        run_dir = storage.run_path(run_id)
        manifest = read_json(run_dir / "manifest.json")
        if manifest.get("status") not in {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}:
            raise HTTPException(
                status_code=409, detail="run result package is available after the run reaches a terminal state"
            )
        from .. import snapshots as snapshot_store

        for item in manifest.get("snapshots", []):
            step_id = item.get("stepId")
            if isinstance(step_id, str):
                snapshot_store.ensure_materialized(run_dir, step_id)
        entries = _archive_entries(run_dir, manifest)
        filename = f"viennaps-run-{run_id}.zip"
        return StreamingResponse(
            stream_zip_async(entries),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except EngineVersionMismatch as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/api/runs/{run_id}/v2")
async def run_v2_get(run_id: str) -> dict:
    """Return the v2 plan/output/state records for one run, if present."""
    from ..v2_records import V2_RECORDS_VERSION, read_v2_steps

    return {
        "schemaVersion": V2_RECORDS_VERSION,
        "runId": run_id,
        "steps": read_v2_steps(storage.run_path(run_id)),
    }
