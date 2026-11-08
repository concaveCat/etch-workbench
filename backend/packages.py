"""Portable continuation packages: export and import.

A package is a ZIP32 stream with ``project.json``, an optional
``domain-state.vpsd`` (native Domain state), an optional content-addressed
``assets/<sha256>.gds`` and an optional ``replay-project.json``. Export streams
from the run/data directories; import validates the archive, verifies every
SHA-256 against the project metadata, registers the native state and returns
the normalized project plus the packaged replay document.

Both entry points keep the exact status codes, ``detail`` strings, entry names
and filename format of the former ``main.py`` routes.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from . import storage
from .api_errors import project_or_422, validation_error_response
from .models import parse_project, parse_replay
from .runtime_limits import artifact_limit_bytes, artifact_limit_label
from .streamzip import stream_zip_async

API_GDS_MAX_BODY = 64 * 1024 * 1024


def project_gds_asset(geometry: Any) -> dict[str, Any] | None:
    if not isinstance(geometry, dict):
        return None
    if geometry.get("kind") == "gds":
        return geometry
    mask = geometry.get("gdsMask")
    return mask if isinstance(mask, dict) else None


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def build_export(payload: dict[str, Any]) -> StreamingResponse:
    if not isinstance(payload, dict) or set(payload) not in ({"project"}, {"project", "replay"}):
        raise HTTPException(status_code=422, detail="body must contain project and optional replay")
    _project, normalized, _warnings = project_or_422(payload["project"])
    if any(step.get("model") == "csvProcess" for step in normalized.get("steps", [])):
        raise HTTPException(
            status_code=422,
            detail="portable project packages do not include uploaded CSV rate assets yet; "
            "save and replay the project in this workbench",
        )
    reference = normalized.get("continuationState")
    gds_geometry = normalized.get("geometry", {})
    gds_asset = project_gds_asset(gds_geometry)
    gds_asset_id = gds_asset.get("assetId") if gds_asset else None
    gds_source = None
    if reference is None and gds_asset_id is None:
        raise HTTPException(
            status_code=422, detail="portable project packages require a GDS asset or a saved native Domain state"
        )
    source = None
    if reference is not None:
        if normalized.get("schemaVersion") not in (6, 7):
            raise HTTPException(
                status_code=422, detail="portable native state packages require a schemaVersion 6 or 7 project"
            )
        if reference["viennapsVersion"] != _version("viennaps") or reference["viennalsVersion"] != _version("viennals"):
            raise HTTPException(
                status_code=409,
                detail="native Domain state engine versions do not match the installed ViennaPS/ViennaLS",
            )
        try:
            source = storage.resolve_domain_state(reference)
            storage.register_domain_state(source, reference["stateId"])
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(
                status_code=409, detail="referenced native Domain state is unavailable or has a SHA-256 mismatch"
            ) from exc
    if gds_asset_id is not None:
        try:
            gds_source = storage.resolve_gds_asset(gds_asset_id)
        except (FileNotFoundError, OSError) as exc:
            raise HTTPException(
                status_code=409, detail="referenced GDS asset is unavailable or has a SHA-256 mismatch"
            ) from exc

    replay_document = None
    if "replay" in payload:
        try:
            replay = parse_replay(payload["replay"])
        except ValidationError as exc:
            return validation_error_response(exc)
        replay_project = replay.normalizedProject.model_dump(mode="json", exclude_none=True)
        if reference is not None:
            if (
                replay.schemaVersion not in (6, 7)
                or replay.normalizedProject.continuationState is None
                or replay.normalizedProject.continuationState.stateId != reference["stateId"]
            ):
                raise HTTPException(
                    status_code=422, detail="replay package must reference the same native Domain state"
                )
        elif (project_gds_asset(replay_project.get("geometry")) or {}).get("assetId") != gds_asset_id:
            raise HTTPException(status_code=422, detail="replay package must reference the same GDS source asset")
        if replay.executionRequirements.viennapsVersion != _version(
            "viennaps"
        ) or replay.executionRequirements.viennalsVersion != _version("viennals"):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"replay requires ViennaPS {replay.executionRequirements.viennapsVersion} / "
                    f"ViennaLS {replay.executionRequirements.viennalsVersion}; installed is "
                    f"{_version('viennaps')} / {_version('viennals')}. Old runs are not reinterpreted under a different runtime."
                ),
            )
        replay_document = replay.model_dump(mode="json", exclude_none=True)
        if replay_document["normalizedProject"].get("continuationState") is not None:
            replay_document["normalizedProject"]["continuationState"].pop("sourceRunId", None)
            replay_document["normalizedProject"]["continuationState"].pop("sourceStepId", None)

    if normalized.get("continuationState") is not None:
        normalized["continuationState"].pop("sourceRunId", None)
        normalized["continuationState"].pop("sourceStepId", None)
    temp_dir = Path(tempfile.mkdtemp(prefix="continuation-package-", dir=storage.VAR))
    project_file = temp_dir / "project.json"
    project_file.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    entries = [("project.json", project_file)]
    if source is not None:
        entries.append(("domain-state.vpsd", source))
    if gds_source is not None:
        entries.append((f"assets/{gds_asset_id}.gds", gds_source))
    if replay_document is not None:
        replay_file = temp_dir / "replay-project.json"
        replay_file.write_text(
            json.dumps(replay_document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        entries.append(("replay-project.json", replay_file))
    package_key = reference["stateId"][:12] if reference is not None else gds_asset_id[:12]
    filename = f"viennaps-project-{package_key}.zip"
    return StreamingResponse(
        stream_zip_async(entries),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
    )


async def import_package(request: Request) -> dict[str, Any]:
    archive_limit = artifact_limit_bytes()
    archive_label = artifact_limit_label()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > archive_limit:
                raise HTTPException(
                    status_code=413, detail=f"continuation package exceeds the {archive_label} archive limit"
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    temp_dir = Path(tempfile.mkdtemp(prefix="continuation-import-", dir=storage.VAR))
    archive_path = temp_dir / "upload.zip"
    try:
        size = 0
        with archive_path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > archive_limit:
                    raise HTTPException(
                        status_code=413, detail=f"continuation package exceeds the {archive_label} archive limit"
                    )
                output.write(chunk)
        if not size:
            raise HTTPException(status_code=400, detail="continuation package is empty")
        try:
            archive = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=422, detail="continuation package is not a valid ZIP archive") from exc
        with archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            asset_names = [name for name in names if re.fullmatch(r"assets/[a-f0-9]{64}\.gds", name)]
            allowed = {"project.json", "domain-state.vpsd", "replay-project.json", *asset_names}
            if (
                len(names) != len(set(names))
                or "project.json" not in names
                or set(names) - allowed
                or len(asset_names) > 1
            ):
                raise HTTPException(
                    status_code=422,
                    detail="package must contain project.json and only known native state, replay, and content-addressed GDS entries",
                )
            if sum(item.file_size for item in infos) > archive_limit:
                raise HTTPException(
                    status_code=413, detail=f"expanded continuation package exceeds the {archive_label} archive limit"
                )
            by_name = {item.filename: item for item in infos}
            for item in infos:
                mode = (item.external_attr >> 16) & 0xFFFF
                if item.is_dir() or stat.S_ISLNK(mode) or item.flag_bits & 1:
                    raise HTTPException(
                        status_code=422, detail="directories, symlinks and encrypted ZIP entries are not allowed"
                    )
            project_info = by_name["project.json"]
            try:
                project_payload = json.loads(
                    archive.read(project_info).decode("utf-8"),
                    parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {item}")),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="project.json is not valid UTF-8 JSON") from exc
            if not isinstance(project_payload, dict):
                raise HTTPException(status_code=422, detail="package project must be a JSON object")
            try:
                parsed_project = parse_project(project_payload)
            except ValidationError as exc:
                return validation_error_response(exc)
            normalized_pre_asset = parsed_project.model_dump(mode="json", exclude_none=True)
            geometry = normalized_pre_asset.get("geometry", {})
            gds_asset = project_gds_asset(geometry)
            asset_id = gds_asset.get("assetId") if gds_asset else None
            state_payload = project_payload.get("continuationState")
            if state_payload is not None and (
                project_payload.get("schemaVersion") not in (6, 7) or not isinstance(state_payload, dict)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="native Domain state packages require schemaVersion 6 or 7 and a valid continuationState",
                )
            expected_asset_name = f"assets/{asset_id}.gds" if asset_id is not None else None
            if (asset_id is not None and asset_names != [expected_asset_name]) or (asset_id is None and asset_names):
                raise HTTPException(
                    status_code=422, detail="package GDS asset path must exactly match the project assetId"
                )
            if state_payload is not None and "domain-state.vpsd" not in names:
                raise HTTPException(status_code=422, detail="project continuationState requires domain-state.vpsd")
            if state_payload is None and "domain-state.vpsd" in names:
                raise HTTPException(
                    status_code=422, detail="package native Domain state is not referenced by project.json"
                )
            if state_payload is None and asset_id is None:
                raise HTTPException(
                    status_code=422, detail="package must contain a GDS asset or a referenced native Domain state"
                )
            if state_payload is not None:
                project_payload["continuationState"].pop("sourceRunId", None)
                project_payload["continuationState"].pop("sourceStepId", None)
            if asset_id is not None:
                asset_info = by_name[expected_asset_name]
                if asset_info.file_size <= 0 or asset_info.file_size > API_GDS_MAX_BODY:
                    raise HTTPException(status_code=413, detail="packaged GDS asset must be between 1 byte and 64 MiB")
                asset_temp = temp_dir / "asset.gds"
                copied = 0
                with archive.open(asset_info, "r") as source_stream, asset_temp.open("wb") as target_stream:
                    while block := source_stream.read(1024 * 1024):
                        copied += len(block)
                        if copied > API_GDS_MAX_BODY:
                            raise HTTPException(status_code=413, detail="expanded GDS asset exceeds the 64 MiB limit")
                        target_stream.write(block)
                if copied != asset_info.file_size or storage.file_sha256(asset_temp) != asset_id:
                    raise HTTPException(
                        status_code=422, detail="packaged GDS asset does not match project geometry.assetId"
                    )
                destination = storage.gds_asset_path(asset_id)
                if destination.exists() and storage.file_sha256(destination) != asset_id:
                    raise HTTPException(
                        status_code=500, detail="content-addressed GDS asset store has a SHA-256 mismatch"
                    )
                if not destination.exists():
                    os.replace(asset_temp, destination)
                from .gds import inspect_gds

                gds_metadata = {
                    "assetId": asset_id,
                    "fileName": gds_asset.get("fileName", "layout.gds"),
                    **inspect_gds(destination.read_bytes()),
                }
                storage.atomic_json(storage.GDS_ASSETS / f"{asset_id}.json", gds_metadata)
            _project, normalized, _warnings = project_or_422(project_payload)
            state_ref = normalized.get("continuationState")
            state_path = None
            if state_ref is not None:
                if state_ref["viennapsVersion"] != _version("viennaps") or state_ref["viennalsVersion"] != _version(
                    "viennals"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="native Domain state engine versions do not match the installed ViennaPS/ViennaLS",
                    )
                state_info = by_name["domain-state.vpsd"]
                if state_info.file_size <= 0:
                    raise HTTPException(status_code=422, detail="domain-state.vpsd is empty")
                state_path = temp_dir / "domain-state.vpsd"
                copied = 0
                with archive.open(state_info, "r") as source_stream, state_path.open("wb") as target_stream:
                    while block := source_stream.read(1024 * 1024):
                        copied += len(block)
                        if copied > archive_limit:
                            raise HTTPException(
                                status_code=413,
                                detail=f"expanded native Domain state exceeds the {archive_label} limit",
                            )
                        target_stream.write(block)
                if copied != state_info.file_size or storage.file_sha256(state_path) != state_ref["stateId"]:
                    raise HTTPException(
                        status_code=422, detail="domain-state.vpsd does not match continuationState.stateId"
                    )
            replay_document = None
            if "replay-project.json" in by_name:
                replay_info = by_name["replay-project.json"]
                try:
                    replay_payload = json.loads(
                        archive.read(replay_info).decode("utf-8"),
                        parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {item}")),
                    )
                    replay = parse_replay(replay_payload)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
                    raise HTTPException(status_code=422, detail="replay-project.json is invalid") from exc
                if state_ref is not None:
                    if (
                        replay.schemaVersion not in (6, 7)
                        or replay.normalizedProject.continuationState is None
                        or replay.normalizedProject.continuationState.stateId != state_ref["stateId"]
                    ):
                        raise HTTPException(
                            status_code=422, detail="replay project must reference the packaged native Domain state"
                        )
                elif (
                    project_gds_asset(
                        replay.normalizedProject.model_dump(mode="json", exclude_none=True).get("geometry")
                    )
                    or {}
                ).get("assetId") != asset_id:
                    raise HTTPException(status_code=422, detail="replay project must reference the packaged GDS asset")
                if replay.executionRequirements.viennapsVersion != _version(
                    "viennaps"
                ) or replay.executionRequirements.viennalsVersion != _version("viennals"):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"replay requires ViennaPS {replay.executionRequirements.viennapsVersion} / "
                            f"ViennaLS {replay.executionRequirements.viennalsVersion}; installed is "
                            f"{_version('viennaps')} / {_version('viennals')}. Old runs are not reinterpreted under a different runtime."
                        ),
                    )
                replay_document = replay.model_dump(mode="json", exclude_none=True)
            if state_path is not None:
                storage.register_domain_state(state_path, state_ref["stateId"])
            return {"project": normalized, "replay": replay_document}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
