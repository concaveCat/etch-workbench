"""Atomic JSON storage rooted exclusively in the workbench data directory."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .runtime_limits import (artifact_limit_bytes, artifact_limit_label, max_data_bytes,
                             max_data_gib, min_free_bytes, min_free_gib)


ROOT = Path(__file__).resolve().parents[1]
VAR = Path(os.environ.get("VIENNAPS_WORKBENCH_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()
PROJECTS = VAR / "projects"
RUNS = VAR / "runs"
DOMAIN_STATES = VAR / "domain-states"
GDS_ASSETS = VAR / "gds-assets"
CSV_ASSETS = VAR / "csv-assets"
LOCK_PATH = VAR / "workbench.lock"
ID_RE = re.compile(r"^[a-f0-9]{24}$")


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream, parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"invalid JSON constant {item}")))


def initialize() -> None:
    for path in (PROJECTS, RUNS, DOMAIN_STATES, GDS_ASSETS, CSV_ASSETS):
        path.mkdir(parents=True, exist_ok=True)
        (path / ".keep").touch(exist_ok=True)
    VAR.mkdir(parents=True, exist_ok=True)


def acquire_instance_lock() -> int:
    initialize()
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError("another ViennaPS Workbench server already holds the data-directory lock")
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd


def checked_id(identifier: str) -> str:
    if not ID_RE.fullmatch(identifier):
        raise FileNotFoundError("unknown project or run ID")
    return identifier


def project_path(identifier: str) -> Path:
    return PROJECTS / f"{checked_id(identifier)}.json"


def run_path(identifier: str) -> Path:
    return RUNS / checked_id(identifier)


def domain_state_path(state_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", state_id):
        raise FileNotFoundError("invalid native Domain state ID")
    return DOMAIN_STATES / f"{state_id}.vpsd"


def gds_asset_path(asset_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
        raise FileNotFoundError("invalid GDS asset ID")
    return GDS_ASSETS / f"{asset_id}.gds"


def resolve_gds_asset(asset_id: str) -> Path:
    path = gds_asset_path(asset_id)
    if not path.is_file() or file_sha256(path) != asset_id:
        raise FileNotFoundError("GDS asset is unavailable or its SHA-256 does not match")
    return path


def csv_asset_path(asset_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
        raise FileNotFoundError("invalid CSV rate asset ID")
    return CSV_ASSETS / f"{asset_id}.csv"


def csv_asset_metadata_path(asset_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
        raise FileNotFoundError("invalid CSV rate asset ID")
    return CSV_ASSETS / f"{asset_id}.json"


def resolve_csv_asset(asset_id: str) -> Path:
    path = csv_asset_path(asset_id)
    if not path.is_file() or file_sha256(path) != asset_id:
        raise FileNotFoundError("CSV rate asset is unavailable or its SHA-256 does not match")
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_domain_state(reference: dict[str, Any]) -> Path:
    state_id = reference.get("stateId")
    if not isinstance(state_id, str) or not re.fullmatch(r"[a-f0-9]{64}", state_id):
        raise FileNotFoundError("invalid native Domain state reference")
    cached = domain_state_path(state_id)
    if cached.is_file():
        if file_sha256(cached) == state_id:
            return cached
        raise OSError("cached native Domain state SHA-256 mismatch")
    run_id = reference.get("sourceRunId")
    step_id = reference.get("sourceStepId")
    if isinstance(run_id, str) and re.fullmatch(r"[a-f0-9]{24}", run_id) and isinstance(step_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step_id):
        candidate = run_path(run_id) / f"state-{step_id}.vpsd"
        if candidate.is_file() and file_sha256(candidate) == state_id:
            return candidate
    raise FileNotFoundError("referenced native Domain state is not available in this workbench")


def register_domain_state(source: Path, state_id: str) -> Path:
    """Store a verified native state under its content hash without absolute references."""
    if file_sha256(source) != state_id:
        raise OSError("native Domain state does not match its SHA-256 reference")
    destination = domain_state_path(state_id)
    if destination.is_file():
        if file_sha256(destination) != state_id:
            raise OSError("stored native Domain state SHA-256 mismatch")
        return destination
    check_disk_preflight(projected_bytes=source.stat().st_size)
    DOMAIN_STATES.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{state_id}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def create_project(project: dict[str, Any]) -> dict[str, Any]:
    import secrets
    identifier = secrets.token_hex(12)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {"id": identifier, "name": project["name"], "updatedAt": now, "project": project}
    atomic_json(project_path(identifier), record)
    return record


def update_project(identifier: str, project: dict[str, Any]) -> dict[str, Any]:
    path = project_path(identifier)
    if not path.is_file():
        raise FileNotFoundError("unknown project ID")
    record = {"id": identifier, "name": project["name"], "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "project": project}
    atomic_json(path, record)
    return record


def list_projects() -> list[dict[str, str]]:
    result = []
    for path in PROJECTS.glob("*.json"):
        if not ID_RE.fullmatch(path.stem):
            continue
        try:
            item = read_json(path)
            result.append({"id": item["id"], "name": item["name"], "updatedAt": item["updatedAt"]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda item: item["updatedAt"], reverse=True)


def directory_bytes(path: Path) -> int:
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            try:
                # Atomic JSON writers leave short-lived ``*.tmp`` files next
                # to their destination. They are not committed artifacts and
                # may be replaced between ``is_file`` and ``stat`` while a
                # supervised worker is saving a snapshot.
                if item.name.endswith(".tmp"):
                    continue
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def app_storage_bytes() -> int:
    return directory_bytes(PROJECTS) + directory_bytes(RUNS) + directory_bytes(DOMAIN_STATES) + directory_bytes(GDS_ASSETS) + directory_bytes(CSV_ASSETS)


def check_disk_preflight(*, projected_bytes: int = 0) -> None:
    usage = shutil.disk_usage(VAR)
    if usage.free < min_free_bytes():
        raise OSError(f"available disk space is below {min_free_gib()} GiB; workbench data is stored at {VAR}")
    if app_storage_bytes() + projected_bytes > max_data_bytes():
        raise OSError(f"workbench data would exceed the {max_data_gib()} GiB storage budget; review data at {VAR}")


def check_artifact_budget(run_dir: Path) -> None:
    if directory_bytes(run_dir) > artifact_limit_bytes():
        raise OSError(f"run artifact size exceeded the {artifact_limit_label()} application cap")

