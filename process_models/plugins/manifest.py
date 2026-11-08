"""Strict manifest parsing for installable process-model plugins.

A manifest is a small TOML file with a fixed field set:

.. code-block:: toml

   api = 1
   id = "isotropic"
   version = "1.0.0"
   entrypoint = "plugins.isotropic:register"
   description = "Native isotropic etch/deposit"

The manifest is the only place a Python module path is allowed. Project JSON
references process/variant IDs only.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import PluginLifecycleError

__all__ = [
    "PLUGIN_API_VERSION",
    "REPO_ROOT",
    "PluginManifest",
    "default_plugin_dirs",
    "parse_entrypoint",
    "read_manifest",
    "resolve_path",
]

#: API version understood by this tree. A manifest with any other value is
#: rejected instead of being loaded with guessed semantics.
PLUGIN_API_VERSION = 1

#: Repository root, used to resolve relative operator paths and the in-tree
#: plugin/ lock locations.
REPO_ROOT = Path(__file__).resolve().parents[2]

PLUGIN_DIRS_ENV = "VIENNAPS_WORKBENCH_PLUGIN_DIRS"

#: Manifest filename inside each plugin directory.
MANIFEST_FILENAME = "plugin.toml"

_REQUIRED_KEYS = frozenset({"api", "id", "version", "entrypoint"})
_ALLOWED_KEYS = _REQUIRED_KEYS | {"description"}
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class PluginManifest:
    """One parsed ``plugin.toml``.

    The identity is ``(id, version)``; the entrypoint is a single
    ``module:attribute`` pair that the loader alone resolves.
    """

    api: int
    id: str
    version: str
    entrypoint: str
    description: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.id, self.version)


def resolve_path(path: str | os.PathLike[str]) -> Path:
    """Resolve ``path`` relative to the repository root."""
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = REPO_ROOT / value
    return value.resolve()


def default_plugin_dirs() -> tuple[Path, ...]:
    """Return the directories scanned for ``*/plugin.toml``.

    ``VIENNAPS_WORKBENCH_PLUGIN_DIRS`` (``os.pathsep`` separated) overrides the
    default ``<repo>/plugins``. Relative entries resolve against the repo root.
    """
    raw = os.environ.get(PLUGIN_DIRS_ENV, "")
    if raw.strip():
        dirs = tuple(resolve_path(part) for part in raw.split(os.pathsep) if part.strip())
        if dirs:
            return dirs
    return (REPO_ROOT / "plugins",)


def parse_entrypoint(entrypoint: str) -> tuple[str, str]:
    """Split and validate a ``module:attribute`` entrypoint.

    Chained attributes (``module:factory.register``), whitespace, empty parts,
    and path-like strings are rejected.
    """
    if not isinstance(entrypoint, str) or not _ENTRYPOINT.match(entrypoint):
        raise PluginLifecycleError(
            f"entrypoint must be exactly one 'module:attribute' pair without spaces, got {entrypoint!r}"
        )
    module, attribute = entrypoint.split(":", 1)
    return module, attribute


def read_manifest(path: str | os.PathLike[str]) -> PluginManifest:
    """Read one strict TOML manifest.

    Unknown keys, a wrong API version, empty identity fields, a malformed
    entrypoint, and any I/O or TOML error raise :class:`PluginLifecycleError`.
    """
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise PluginLifecycleError(f"cannot read plugin manifest {manifest_path}: {error}") from error

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PluginLifecycleError(f"invalid TOML in plugin manifest {manifest_path}: {error}") from error

    if not isinstance(data, dict):
        raise PluginLifecycleError(f"plugin manifest {manifest_path} must be a TOML table")

    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise PluginLifecycleError(f"plugin manifest {manifest_path} has unknown keys: {unknown}")
    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        raise PluginLifecycleError(f"plugin manifest {manifest_path} is missing required keys: {missing}")

    api = data["api"]
    if isinstance(api, bool) or not isinstance(api, int):
        raise PluginLifecycleError(f"plugin manifest {manifest_path}: api must be an integer")
    if api != PLUGIN_API_VERSION:
        raise PluginLifecycleError(
            f"plugin manifest {manifest_path}: unsupported api {api!r}; expected {PLUGIN_API_VERSION}"
        )

    plugin_id = _non_empty_string(data["id"], manifest_path, "id")
    version = _non_empty_string(data["version"], manifest_path, "version")
    entrypoint = _non_empty_string(data["entrypoint"], manifest_path, "entrypoint")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise PluginLifecycleError(f"plugin manifest {manifest_path}: description must be a string")

    parse_entrypoint(entrypoint)
    return PluginManifest(
        api=api,
        id=plugin_id,
        version=version,
        entrypoint=entrypoint,
        description=description,
    )


def _non_empty_string(value: object, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginLifecycleError(f"plugin manifest {path}: {field} must be a non-empty string")
    return value
