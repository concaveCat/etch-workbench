"""FastAPI service for the localhost ViennaPS Workbench."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from . import packages
from . import settings as settings_store
from . import preflight as artifact_preflight
from . import materials as material_registry
from .api.deps import supervisor as _supervisor, version as _version
from .api.runs import (continuation_project_get, flux_result_get, flux_run_create,
                       intermediate_output_get, native_artifact_download,
                       replay_project_get, router as runs_router, run_cancel, run_create,
                       run_download, run_get, run_replay, runs_list, slice_run_create,
                       snapshot_get, tail_router as runs_tail_router)
from .api_errors import project_or_422, validation_error_response
from .models import ProjectV6, ProjectV7
from .packages import API_GDS_MAX_BODY
from .storage import atomic_json, read_json
from .supervisor import Supervisor
from .specialized_models import specialized_catalog
from .runtime_limits import (artifact_limit_startup_message,
                             deployment_limit_status,
                             deployment_limits_startup_message, cpu_thread_limit)
from .gpu_devices import cuda_devices
from .workflow.api import router as workflow_router
from .engine import EngineVersionMismatch, multi_ion_gpu_error


ROOT = storage.ROOT
# Uvicorn configures this logger with an INFO handler; a module logger would
# otherwise fall back to logging.lastResort and drop the startup line.
STARTUP_LOGGER = logging.getLogger("uvicorn.error")
API_CSV_MAX_BODY = 16 * 1024 * 1024
APP_VERSION = "0.1.0"
CAPABILITIES = ROOT / "docs" / "capabilities.json"
RESOURCE_POLICY = ROOT / "docs" / "resource-policy.json"
MATERIALS_CATALOG = ROOT / "docs" / "materials-catalog.json"
FRONTEND_DIST = ROOT / "frontend" / "dist"


# 422 helpers live in api_errors so extracted modules (packages) reuse them
# without importing this module; the aliases keep every existing route working.
_validation_error = validation_error_response
_project_or_422 = project_or_422


class ApiContentTypeMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/") or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        content_length = next((value.decode("latin1") for key, value in scope.get("headers", []) if key.lower() == b"content-length"), None)
        transfer_encoding = next((value.decode("latin1").lower() for key, value in scope.get("headers", []) if key.lower() == b"transfer-encoding"), "")
        path = scope.get("path", "")
        if path == "/api/gds-assets" and scope.get("method") == "POST":
            content_type = next((value.decode("latin1").split(";", 1)[0].strip().lower() for key, value in scope.get("headers", []) if key.lower() == b"content-type"), "")
            if content_type != "application/octet-stream":
                response = JSONResponse(status_code=415, content={"detail": "GDS uploads must use application/octet-stream"})
                await response(scope, receive, send)
                return
            if content_length is not None:
                try:
                    if int(content_length) > API_GDS_MAX_BODY:
                        await self._reject_oversized(scope, receive, send, maximum=API_GDS_MAX_BODY)
                        return
                except ValueError:
                    await self._reject_oversized(scope, receive, send, maximum=API_GDS_MAX_BODY)
                    return
            # The upload route streams into a temporary file and enforces its own cap.
            await self.app(scope, receive, send)
            return
        if path == "/api/csv-assets" and scope.get("method") == "POST":
            content_type = next((value.decode("latin1").split(";", 1)[0].strip().lower() for key, value in scope.get("headers", []) if key.lower() == b"content-type"), "")
            if content_type not in {"text/csv", "text/plain", "application/octet-stream"}:
                response = JSONResponse(status_code=415, content={"detail": "CSV rate uploads must use text/csv or application/octet-stream"})
                await response(scope, receive, send)
                return
            if content_length is not None:
                try:
                    if int(content_length) > API_CSV_MAX_BODY:
                        await self._reject_oversized(scope, receive, send, maximum=API_CSV_MAX_BODY)
                        return
                except ValueError:
                    await self._reject_oversized(scope, receive, send, maximum=API_CSV_MAX_BODY)
                    return
            await self.app(scope, receive, send)
            return
        if path == "/api/continuation-package/import" and scope.get("method") == "POST":
            content_type = next((value.decode("latin1").split(";", 1)[0].strip().lower() for key, value in scope.get("headers", []) if key.lower() == b"content-type"), "")
            if content_type != "application/zip":
                response = JSONResponse(status_code=415, content={"detail": "continuation packages must use application/zip"})
                await response(scope, receive, send)
                return
            # The route consumes and caps the body incrementally; do not buffer a
            # potentially large native state package in middleware memory.
            await self.app(scope, receive, send)
            return
        bodyless_cancel = scope.get("method") == "POST" and (
            (path.startswith("/api/runs/") and path.endswith("/cancel"))
            or (path.startswith("/api/v1/workflow/") and path.endswith("/cancel"))
        )
        if bodyless_cancel and content_length in (None, "0") and not transfer_encoding:
            await self.app(scope, receive, send)
            return
        content_type = next((value.decode("latin1").split(";", 1)[0].strip().lower() for key, value in scope.get("headers", []) if key.lower() == b"content-type"), "")
        if content_type != "application/json":
            response = JSONResponse(status_code=415, content={"detail": "API request bodies must use application/json"})
            await response(scope, receive, send)
            return
        # General JSON request bodies have no fixed byte cap. Let the route
        # consume them directly without buffering a second copy in middleware.
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject_oversized(scope: dict[str, Any], receive: Any, send: Any, *, maximum: int) -> None:
        response = JSONResponse(status_code=413, content={"detail": f"request body exceeds the {maximum // (1024 * 1024)} MiB limit"})
        await response(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    STARTUP_LOGGER.info("%s", artifact_limit_startup_message())
    STARTUP_LOGGER.info("%s", deployment_limits_startup_message())
    lock_fd = storage.acquire_instance_lock()
    supervisor = Supervisor(lock_fd)
    app.state.supervisor = supervisor
    try:
        from .workflow.api import get_store
        from .workflow.experiments import resume_experiments
        resume_experiments(get_store(), supervisor)
    except Exception:
        STARTUP_LOGGER.exception("experiment queue recovery failed")
    try:
        yield
    finally:
        # Lifespan shutdown is already draining requests. Keeping close
        # synchronous avoids leaving a default-executor thread behind while
        # the server's event loop is being destroyed.
        supervisor.close()


app = FastAPI(title="ViennaPS Workbench", version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(ApiContentTypeMiddleware)
app.include_router(workflow_router)


@app.middleware("http")
async def localhost_only(request: Request, call_next: Any):
    port = int(os.environ.get("WORKBENCH_PORT", "8767"))
    host_header = request.headers.get("host", "").strip().lower()
    expected_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host_header not in expected_hosts:
        return JSONResponse(status_code=400, content={"detail": "request Host must be the active loopback address and port"})
    origin = request.headers.get("origin")
    if origin:
        expected_origin = f"http://{host_header}"
        if origin.rstrip("/").lower() != expected_origin:
            return JSONResponse(status_code=403, content={"detail": "cross-origin requests are not allowed"})
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    supervisor = _supervisor(request)
    active = None
    if supervisor.active and supervisor._status(supervisor.active.run_id) in {"pending", "running", "cancelling"}:
        active = supervisor.active.run_id
    return {
        "status": "ok",
        "applicationVersion": APP_VERSION,
        "python": sys.version.split()[0],
        "viennaps": _version("viennaps"),
        "viennals": _version("viennals"),
        "platform": platform.platform(),
        "activeRunId": active,
    }


@app.get("/api/capabilities")
async def capabilities() -> dict[str, Any]:
    capability_data = read_json(CAPABILITIES)
    resource_policy = read_json(RESOURCE_POLICY)
    limits = deployment_limit_status()
    resource_policy["run"].update({
        "maximumArtifactBytes": limits["artifactLimitBytes"],
        "maximumResultArchiveBytes": limits["artifactLimitBytes"],
        **{key: value for key, value in limits.items() if key.startswith("artifactLimit")},
        "rssTerminationMiB": limits["rssLimitMiB"],
        "maximumCombinedStdoutStderrBytes": limits["maximumLogBytes"],
    })
    resource_policy["run"]["artifactPreflight"].update({
        "capBytes": limits["artifactLimitBytes"],
        "capMiB": limits["artifactLimitMiB"],
        "artifactLimitSource": limits["artifactLimitSource"],
    })
    resource_policy["run"]["storage"].update({
        "maximumWorkbenchBytes": limits["maximumDataBytes"],
        "maximumWorkbenchGiB": limits["maximumDataGiB"],
        "maximumWorkbenchSource": limits["maximumDataSource"],
        "maximumWorkbenchEnvironmentVariable": limits["maximumDataEnvironmentVariable"],
        "maxDataDefaultGiB": limits["maximumDataDefaultGiB"],
        "maxDataMinimumGiB": limits["maximumDataMinimumGiB"],
        "maxDataMaximumGiB": limits["maximumDataMaximumGiB"],
        "minimumFreeBytes": limits["minimumFreeBytes"],
        "minimumFreeGiB": limits["minimumFreeGiB"],
        "minimumFreeSource": limits["minimumFreeSource"],
        "minimumFreeEnvironmentVariable": limits["minimumFreeEnvironmentVariable"],
        "minFreeDefaultGiB": limits["minimumFreeDefaultGiB"],
        "minFreeMinimumGiB": limits["minimumFreeMinimumGiB"],
        "minFreeMaximumGiB": limits["minimumFreeMaximumGiB"],
    })
    resource_policy["run"]["cpuThreads"].update(cpu_thread_limit())
    try:
        gpu_matrix = read_json(ROOT / "docs" / "stage9-evidence" / "gpu-capability-matrix.json")
        capability_data["gpuModelRows"] = gpu_matrix.get("rows", [])
        capability_data["knownGpuProfileFailures"] = gpu_matrix.get("knownGpuProfileFailures", [])
        capability_data["knownNativeFailures"] = gpu_matrix.get("knownNativeFailures", [])
        capability_data["knownEngineLimitations"] = gpu_matrix.get("knownEngineLimitations", [])
    except (OSError, ValueError, AttributeError):
        capability_data["gpuModelRows"] = []
        capability_data["knownGpuProfileFailures"] = []
        capability_data["knownNativeFailures"] = []
        capability_data["knownEngineLimitations"] = []
    return {"capabilities": capability_data, "resourcePolicy": resource_policy,
            "runtimeLimits": limits}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    """Return persistent limits, storage usage, and installed engine versions."""
    return settings_store.settings_response()


@app.put("/api/settings", responses={
    422: {
        "description": "Validation Error",
        "content": {"application/json": {"schema": {
            "type": "object",
            "required": ["detail"],
            "properties": {"detail": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "message"],
                    "properties": {
                        "path": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            }},
        }}},
    },
})
async def put_settings(payload: Any = Body(...)) -> dict[str, Any]:
    """Update limit overrides; ``null`` removes an override."""
    try:
        return settings_store.apply_update(payload)
    except settings_store.SettingsValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.issues) from exc


@app.get("/api/gpu-devices")
def gpu_devices() -> dict[str, Any]:
    """Return CUDA-capable NVIDIA devices seen by a bounded host probe."""
    return cuda_devices()


@app.get("/api/materials")
async def materials() -> dict[str, Any]:
    try:
        catalog = material_registry.material_catalog()
        rows = catalog.get("materials")
        if not isinstance(rows, list) or any(not isinstance(row.get("id"), int) or not isinstance(row.get("name"), str) or not isinstance(row.get("color"), str) for row in rows):
            raise ValueError("invalid catalog entries")
        return catalog
    except (OSError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail="verified native material catalog is unavailable") from exc


@app.get("/api/engines")
async def engine_plugins() -> dict[str, Any]:
    """Return the installed engine plugins and their advertised capabilities."""
    from process_models.engine_plugins import EngineContext, load_engine_registry

    registry = load_engine_registry()
    provider = registry.provider(EngineContext(project={}, run_dir=ROOT, fingerprint="engine-catalog"))
    return {
        "engines": [
            {
                "engineId": capability.engine_id,
                "version": capability.version,
                "kind": capability.kind,
                "backends": list(capability.backends),
                "capabilities": list(capability.capabilities),
                "kernels": list(capability.kernels),
                "devices": list(capability.devices),
            }
            for capability in provider.engines()
        ]
    }


@app.get("/api/specialized-models")
async def specialized_models() -> dict[str, Any]:
    """Return only the checked ViennaPS 4.6.2 native model allowlist."""
    return specialized_catalog()


@app.post("/api/materials", status_code=201)
async def material_create(payload: Any = Body(...)) -> dict[str, Any]:
    try:
        return material_registry.create_material(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"path": "material", "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.patch("/api/materials/{name}")
async def material_metadata_update(name: str, payload: Any = Body(...)) -> dict[str, Any]:
    try:
        return material_registry.update_material(name, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="custom material not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"path": "metadata", "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.delete("/api/materials/{name}")
async def material_delete(name: str, payload: Any = Body(...)) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"project"} or not isinstance(payload["project"], dict):
        raise HTTPException(status_code=422, detail="body must contain the current project")
    try:
        deleted = material_registry.delete_material(name, payload["project"])
        return {"deleted": True, "name": deleted["name"], "id": deleted["id"]}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="custom material not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.post("/api/gds-assets", status_code=201)
async def gds_asset_upload(request: Request, file_name: str = Query(default="layout.gds", min_length=1, max_length=255)) -> dict[str, Any]:
    from .gds import GdsFormatError, inspect_gds, parse_gds
    if "/" in file_name or "\\" in file_name or any(ord(value) < 32 for value in file_name):
        raise HTTPException(status_code=422, detail="GDS file name must be a simple file name")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > API_GDS_MAX_BODY:
                raise HTTPException(status_code=413, detail="GDS file exceeds the 64 MiB upload limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    storage.initialize()
    temp_dir = Path(tempfile.mkdtemp(prefix="gds-upload-", dir=storage.VAR))
    upload_path = temp_dir / "upload.gds"
    size = 0
    digest = __import__("hashlib").sha256()
    try:
        with upload_path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > API_GDS_MAX_BODY:
                    raise HTTPException(status_code=413, detail="GDS file exceeds the 64 MiB upload limit")
                digest.update(chunk)
                output.write(chunk)
        try:
            data = upload_path.read_bytes()
            library = parse_gds(data)
            summary = inspect_gds(data)
        except (OSError, GdsFormatError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"GDSII file is invalid or unsupported: {exc}") from exc
        asset_id = digest.hexdigest()
        destination = storage.gds_asset_path(asset_id)
        if destination.exists():
            if storage.file_sha256(destination) != asset_id:
                raise HTTPException(status_code=500, detail="content-addressed GDS asset store has a SHA-256 mismatch")
        else:
            storage.check_disk_preflight(projected_bytes=size)
            os.replace(upload_path, destination)
        metadata = {"assetId": asset_id, "fileName": file_name, **summary}
        atomic_json(storage.GDS_ASSETS / f"{asset_id}.json", metadata)
        return metadata
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.get("/api/gds-assets/{asset_id}")
async def gds_asset_get(asset_id: str) -> dict[str, Any]:
    from .gds import inspect_gds
    try:
        path = storage.resolve_gds_asset(asset_id)
        metadata_path = storage.GDS_ASSETS / f"{asset_id}.json"
        metadata = read_json(metadata_path) if metadata_path.is_file() else {"fileName": path.name}
        return {**metadata, "assetId": asset_id, **inspect_gds(path.read_bytes())}
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="GDS asset not found or SHA-256 mismatch") from exc


@app.get("/api/csv-assets/{asset_id}")
async def csv_asset_get(asset_id: str) -> dict[str, Any]:
    from .csv_assets import canonicalize_rate_csv
    try:
        path = storage.resolve_csv_asset(asset_id)
        metadata_path = storage.csv_asset_metadata_path(asset_id)
        metadata = read_json(metadata_path) if metadata_path.is_file() else {}
        dimension = metadata.get("dimension")
        if type(dimension) is not int or dimension not in (2, 3):
            raise ValueError("CSV asset metadata does not declare a 2D or 3D rate table")
        raw = path.read_bytes()
        canonical, summary = canonicalize_rate_csv(raw, dimension)
        if canonical != raw:
            raise ValueError("stored CSV asset is not in canonical form; re-upload it")
        return {**metadata, **summary, "assetId": asset_id,
                "sourceSizeBytes": metadata.get("sourceSizeBytes", summary["sourceSizeBytes"])}
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="CSV rate asset not found, invalid or SHA-256 mismatch") from exc


@app.post("/api/csv-assets", status_code=201)
async def csv_asset_upload(request: Request,
                           file_name: str = Query(default="rates.csv", min_length=1, max_length=255),
                           dimension: int = Query(...)) -> dict[str, Any]:
    from .csv_assets import RateCsvError, canonicalize_rate_csv
    if dimension not in (2, 3):
        raise HTTPException(status_code=422, detail="CSV rate tables require dimension=2 or dimension=3")
    if "/" in file_name or "\\" in file_name or any(ord(value) < 32 or ord(value) == 127 for value in file_name):
        raise HTTPException(status_code=422, detail="CSV file name must be a simple file name without control characters")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > API_CSV_MAX_BODY:
                raise HTTPException(status_code=413, detail="CSV rate table exceeds the 16 MiB upload limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    storage.initialize()
    temp_dir = Path(tempfile.mkdtemp(prefix="csv-upload-", dir=storage.VAR))
    upload_path = temp_dir / "upload.csv"
    size = 0
    digest = hashlib.sha256()
    try:
        with upload_path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > API_CSV_MAX_BODY:
                    raise HTTPException(status_code=413, detail="CSV rate table exceeds the 16 MiB upload limit")
                digest.update(chunk)
                output.write(chunk)
        data = upload_path.read_bytes()
        source_sha256 = digest.hexdigest()
        if source_sha256 != hashlib.sha256(data).hexdigest():
            raise HTTPException(status_code=500, detail="CSV upload SHA-256 changed while streaming")
        try:
            canonical, summary = canonicalize_rate_csv(data, dimension)
        except (OSError, RateCsvError) as exc:
            raise HTTPException(status_code=422, detail=f"CSV rate table is invalid: {exc}") from exc
        asset_id = summary["canonicalSha256"]
        if asset_id != hashlib.sha256(canonical).hexdigest():
            raise HTTPException(status_code=500, detail="canonical CSV SHA-256 is inconsistent")
        destination = storage.csv_asset_path(asset_id)
        if destination.exists():
            if storage.file_sha256(destination) != asset_id:
                raise HTTPException(status_code=500, detail="content-addressed CSV asset store has a SHA-256 mismatch")
        else:
            storage.check_disk_preflight(projected_bytes=len(canonical))
            upload_path.write_bytes(canonical)
            os.replace(upload_path, destination)
        metadata = {"assetId": asset_id, "fileName": file_name, "sourceSha256": source_sha256, **summary}
        atomic_json(storage.csv_asset_metadata_path(asset_id), metadata)
        return metadata
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/gds-assets/{asset_id}/inspect")
async def gds_asset_inspect(asset_id: str, payload: Any = Body(...)) -> dict[str, Any]:
    from .gds import GdsFormatError, parse_gds
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="inspection body must be a JSON object")
    try:
        source = storage.resolve_gds_asset(asset_id)
        library = parse_gds(source.read_bytes())
        structure = payload["structure"]
        available = library.effective_layers(structure)
        available_layer_datatypes = set(library.effective_layer_datatypes(structure))
        dimension = payload["dimension"]
        delta = float(payload["gridDelta"])
        unit_scale = float(payload.get("unitScale", 1.0))
        position = payload.get("position", [0.0, 0.0])
        selected_layers = payload.get("selectedLayers", available)
        selected_layer_datatypes = payload.get("selectedLayerDatatypes")
        boundary = payload.get("boundary", "reflective")
        x_extent = payload.get("xExtent")
        y_extent = payload.get("yExtent")
        probe_height = float(payload.get("probeHeight", max(delta * 2, 1.0)))
        if type(dimension) is not int or dimension not in (2, 3) or not isinstance(structure, str):
            raise ValueError("dimension must be 2 or 3 and structure must be a string")
        if not (0 < delta <= 0.5) or not (0 < unit_scale <= 1_000_000):
            raise ValueError("gridDelta or GDS unit scale is outside the application range")
        if boundary not in ("reflective", "periodic") or not isinstance(position, list) or len(position) != 2:
            raise ValueError("boundary or GDS XY placement is invalid")
        if x_extent is not None and (not isinstance(x_extent, (int, float)) or isinstance(x_extent, bool)
                                     or not __import__("math").isfinite(float(x_extent)) or float(x_extent) <= 0):
            raise ValueError("xExtent must be positive and finite")
        if y_extent is not None and (not isinstance(y_extent, (int, float)) or isinstance(y_extent, bool)
                                     or not __import__("math").isfinite(float(y_extent)) or float(y_extent) <= 0):
            raise ValueError("yExtent must be positive and finite")
        if boundary == "periodic" and dimension == 3 and (x_extent is None or y_extent is None):
            raise ValueError("periodic 3D GDS inspection requires xExtent and yExtent")
        if not isinstance(selected_layers, list) or not selected_layers or any(type(value) is not int for value in selected_layers):
            raise ValueError("selectedLayers must be a nonempty list of integer layer IDs")
        if len(set(selected_layers)) != len(selected_layers) or set(selected_layers) - set(available):
            raise ValueError("selectedLayers must be a unique subset of the selected structure's layers")
        if selected_layer_datatypes is not None:
            if (not isinstance(selected_layer_datatypes, list) or not selected_layer_datatypes
                    or any(not isinstance(pair, list) or len(pair) != 2
                           or any(type(value) is not int for value in pair)
                           for pair in selected_layer_datatypes)):
                raise ValueError("selectedLayerDatatypes must be a nonempty list of [layer, datatype] pairs")
            pairs = [tuple(pair) for pair in selected_layer_datatypes]
            if len(set(pairs)) != len(pairs) or set(pairs) - available_layer_datatypes:
                raise ValueError("selectedLayerDatatypes must exist in the selected GDS structure")
            if {layer for layer, _datatype in pairs} - set(selected_layers):
                raise ValueError("selectedLayerDatatypes must belong to selectedLayers")
            non_mask_pairs = set(pairs) - set(library.effective_mask_layer_datatypes(structure))
            if non_mask_pairs:
                formatted = ", ".join(f"{layer}/{datatype}" for layer, datatype in sorted(non_mask_pairs))
                raise ValueError(f"selected GDS layer/datatype pair(s) {formatted} contain no closed boundary polygons and cannot be used as a mask")
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not __import__("math").isfinite(float(value)) for value in position):
            raise ValueError("GDS XY placement must contain finite coordinates")
        if not __import__("math").isfinite(probe_height) or probe_height <= 0:
            raise ValueError("3D probe height must be positive and finite")
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=f"missing inspection input {exc.args[0]}") from exc
    except (FileNotFoundError, GdsFormatError, ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid GDS inspection input: {exc}") from exc

    temp_dir = Path(tempfile.mkdtemp(prefix="gds-inspect-", dir=storage.VAR))
    config_path = temp_dir / "input.json"
    output_path = temp_dir / "native-inspection.json"
    record_path = temp_dir / "child-record.json"
    log_path = temp_dir / "native.log"
    atomic_json(config_path, {"sourcePath": str(source), "assetId": asset_id, "structure": structure,
                              "dimension": dimension, "gridDelta": delta, "boundary": boundary,
                              "xExtent": float(x_extent) if x_extent is not None else None,
                              "yExtent": float(y_extent) if y_extent is not None else None,
                              "unitScale": unit_scale, "position": [float(value) for value in position],
                              "selectedLayers": selected_layers,
                              "selectedLayerDatatypes": selected_layer_datatypes,
                              "probeHeight": probe_height})
    python = str(Path(sys.executable).resolve())
    command = [python, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
               "--record", str(record_path), "--log-path", str(log_path), "--parent-pid", str(os.getpid()),
               "--timeout-seconds", "30", "--max-rss-mib", "2048", "--max-log-bytes", "4194304",
               "--terminate-grace-seconds", "5", "--", python, str(ROOT / "worker/inspect_gds_native.py"),
               str(config_path), str(output_path)]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=40, check=False)
        if result.returncode != 0 or not output_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else result.stderr[-1000:]
            raise HTTPException(status_code=422, detail=f"ViennaPS native GDS inspection failed under bounded_child.py: {log or 'worker returned no inspection result'}")
        report = read_json(output_path)
        guard = read_json(record_path) if record_path.is_file() else {}
        return {"inspection": report, "boundedChild": {"status": guard.get("status"), "returnCode": guard.get("returnCode"),
                                                          "elapsedSeconds": guard.get("elapsedSeconds"), "peakRssBytes": guard.get("peakRssBytes"),
                                                          "logBytes": guard.get("logBytes")}}
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="ViennaPS native GDS inspection exceeded its bounded wall-clock limit") from exc
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.post("/api/projects/validate")
async def validate_endpoint(payload: Any = Body(...)) -> dict[str, Any]:
    project, normalized, warnings = _project_or_422(payload)
    from .hashing import requested_input_hash
    # Advisory only: validation stays 200/valid even when the estimate is over
    # the run cap, so existing clients keep their current behavior. The warning
    # is appended to the existing warning list and the structured estimate is
    # an additive response field.
    estimate = artifact_preflight.estimate_artifacts(normalized)
    warning = artifact_preflight.warning_message(estimate)
    validation_warnings = list(warnings)
    if warning:
        validation_warnings.append(warning)
    return {"valid": True, "warnings": validation_warnings, "requestedInputHash": requested_input_hash(project),
            "normalizedProject": normalized, "artifactEstimate": estimate}


@app.get("/api/projects")
async def projects_list() -> dict[str, Any]:
    return {"projects": storage.list_projects()}


@app.post("/api/projects", status_code=201)
async def project_create(payload: Any = Body(...)) -> dict[str, Any]:
    _project, normalized, _warnings = _project_or_422(payload)
    try:
        storage.check_disk_preflight(projected_bytes=len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")))
        return storage.create_project(normalized)
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
async def project_get(project_id: str) -> dict[str, Any]:
    try:
        record = read_json(storage.project_path(project_id))
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    return {"id": record["id"], "project": record["project"], "updatedAt": record["updatedAt"]}


@app.put("/api/projects/{project_id}")
async def project_update(project_id: str, payload: Any = Body(...)) -> dict[str, Any]:
    _project, normalized, _warnings = _project_or_422(payload)
    try:
        storage.check_disk_preflight(projected_bytes=len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")))
        record = storage.update_project(project_id, normalized)
        return {"id": record["id"], "project": record["project"], "updatedAt": record["updatedAt"]}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc


# The run routes are split across two includes so their registration order
# matches the former inline block: replay-project/download came after the
# continuation-package routes and still do.
app.include_router(runs_router)


@app.post("/api/continuation-package/export")
async def continuation_package_export(payload: Any = Body(...)):
    return packages.build_export(payload)


@app.post("/api/continuation-package/import")
async def continuation_package_import(request: Request) -> dict[str, Any]:
    return await packages.import_package(request)


app.include_router(runs_tail_router)


if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/")
    async def frontend_not_built():
        return JSONResponse(status_code=503, content={"detail": "frontend build is not present; run scripts/build-frontend.sh after installing the locked npm dependencies"})

