"""Strict engine-plugin manifests.

The manifest is the only place an engine provider module path is allowed.
Process JSON never names a module or a provider; it declares an
``EngineRequirement`` and the registry resolves it.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ENGINE_API_VERSION",
    "EngineManifest",
    "default_engine_dirs",
    "parse_entrypoint",
    "read_engine_manifest",
]

ENGINE_API_VERSION = 1
ENGINE_DIRS_ENV = "VIENNAPS_WORKBENCH_ENGINE_DIRS"
MANIFEST_FILENAME = "engine.toml"
_REPO_ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_KEYS = frozenset({"api", "engine_id", "version", "entrypoint"})
_ALLOWED_KEYS = _REQUIRED_KEYS | {"description"}
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class EngineManifest:
    """One parsed ``engine.toml``."""

    api: int
    engine_id: str
    version: str
    entrypoint: str
    description: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.engine_id, self.version)


def default_engine_dirs() -> tuple[Path, ...]:
    """Return operator-controlled engine-plugin roots.

    ``VIENNAPS_WORKBENCH_ENGINE_DIRS`` (os.pathsep separated) overrides the
    built-in ``<repo>/engine_plugins`` directory.
    """
    raw = os.environ.get(ENGINE_DIRS_ENV, "")
    if raw.strip():
        dirs = tuple(_resolve_path(part) for part in raw.split(os.pathsep) if part.strip())
        if dirs:
            return dirs
    return (_REPO_ROOT / "engine_plugins",)


def parse_entrypoint(entrypoint: str) -> tuple[str, str]:
    if not isinstance(entrypoint, str) or not _ENTRYPOINT.match(entrypoint):
        raise ValueError(f"engine entrypoint must be exactly one 'module:attribute' pair, got {entrypoint!r}")
    module, attribute = entrypoint.split(":", 1)
    return module, attribute


def read_engine_manifest(path: str | os.PathLike[str]) -> EngineManifest:
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read engine manifest {manifest_path}: {error}") from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"invalid TOML in engine manifest {manifest_path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"engine manifest {manifest_path} must be a TOML table")
    unknown = sorted(set(data) - _ALLOWED_KEYS)
    missing = sorted(_REQUIRED_KEYS - set(data))
    if unknown:
        raise ValueError(f"engine manifest {manifest_path} has unknown keys: {unknown}")
    if missing:
        raise ValueError(f"engine manifest {manifest_path} is missing required keys: {missing}")
    api = data["api"]
    if isinstance(api, bool) or not isinstance(api, int) or api != ENGINE_API_VERSION:
        raise ValueError(f"engine manifest {manifest_path}: api must be {ENGINE_API_VERSION}")
    engine_id = _non_empty(data["engine_id"], manifest_path, "engine_id")
    version = _non_empty(data["version"], manifest_path, "version")
    entrypoint = _non_empty(data["entrypoint"], manifest_path, "entrypoint")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"engine manifest {manifest_path}: description must be a string")
    parse_entrypoint(entrypoint)
    return EngineManifest(api, engine_id, version, entrypoint, description)


def _resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def _non_empty(value: object, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"engine manifest {path}: {field} must be a non-empty string")
    return value
