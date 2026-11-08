"""JSON lock schema for installed process-model plugins.

``plugins.lock`` is the checked-in, reviewable record of exactly which plugin
versions may load. It pins the manifest bytes, API, entrypoint, and every
variant's implementation hash. Loading compares live manifests and modules
against the lock and refuses to fall back on drift.

Schema version 1:

.. code-block:: json

   {
     "schemaVersion": 1,
     "apiVersion": 1,
     "plugins": [
       {
         "id": "isotropic",
         "version": "1.0.0",
         "api": 1,
         "entrypoint": "plugins.isotropic:register",
         "manifestSha256": "<sha256 of plugin.toml bytes>",
         "variants": {
           "native": {
             "version": "1.0.0",
             "implementationHash": "<registry compute_implementation_hash>"
           }
         }
       }
     ]
   }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PluginLifecycleError
from .manifest import PLUGIN_API_VERSION, REPO_ROOT, parse_entrypoint, resolve_path

__all__ = [
    "LOCK_FILENAME",
    "SCHEMA_VERSION",
    "LockedPlugin",
    "LockedVariant",
    "PluginLock",
    "default_lock_path",
    "read_lock",
    "write_lock_file",
]

SCHEMA_VERSION = 1
LOCK_FILENAME = "plugins.lock"

LOCK_ENV = "VIENNAPS_WORKBENCH_PLUGIN_LOCK"

_TOP_LEVEL_KEYS = frozenset({"schemaVersion", "apiVersion", "plugins"})
_PLUGIN_KEYS = frozenset({"id", "version", "api", "entrypoint", "manifestSha256", "variants"})
_VARIANT_KEYS = frozenset({"version", "implementationHash"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LockedVariant:
    """One locked variant identity and implementation hash."""

    version: str
    implementation_hash: str


@dataclass(frozen=True)
class LockedPlugin:
    """One locked ``(id, version)`` plugin entry."""

    id: str
    version: str
    api: int
    entrypoint: str
    manifest_sha256: str
    variants: dict[str, LockedVariant] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.id, self.version)


@dataclass(frozen=True)
class PluginLock:
    """The parsed lock file, sorted by ``(id, version)`` when written."""

    plugins: tuple[LockedPlugin, ...]
    schema_version: int = SCHEMA_VERSION
    api_version: int = PLUGIN_API_VERSION

    def entries(self) -> dict[tuple[str, str], LockedPlugin]:
        return {plugin.key: plugin for plugin in self.plugins}

    def payload(self) -> dict[str, Any]:
        """Return the canonical JSON payload (sorted, no trailing bytes)."""
        plugins = []
        for plugin in sorted(self.plugins, key=lambda item: item.key):
            variants = {
                variant_id: {
                    "version": value.version,
                    "implementationHash": value.implementation_hash,
                }
                for variant_id, value in sorted(plugin.variants.items())
            }
            plugins.append(
                {
                    "id": plugin.id,
                    "version": plugin.version,
                    "api": plugin.api,
                    "entrypoint": plugin.entrypoint,
                    "manifestSha256": plugin.manifest_sha256,
                    "variants": variants,
                }
            )
        return {
            "schemaVersion": self.schema_version,
            "apiVersion": self.api_version,
            "plugins": plugins,
        }


def default_lock_path() -> Path:
    """Return the lock path (``VIENNAPS_WORKBENCH_PLUGIN_LOCK`` overrides)."""
    override = os.environ.get(LOCK_ENV, "")
    if override.strip():
        return resolve_path(override)
    return REPO_ROOT / LOCK_FILENAME


def read_lock(path: str | os.PathLike[str] | None = None) -> PluginLock:
    """Read and strictly validate the lock file.

    A missing file, malformed JSON, unknown keys, a wrong schema/API version,
    or a malformed identity raises :class:`PluginLifecycleError`.
    """
    lock_path = Path(path) if path is not None else default_lock_path()
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PluginLifecycleError(f"cannot read plugin lock {lock_path}: {error}") from error
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise PluginLifecycleError(f"invalid JSON in plugin lock {lock_path}: {error}") from error

    return _parse_lock(raw, lock_path)


def write_lock_file(lock: PluginLock, path: str | os.PathLike[str] | None = None) -> Path:
    """Write ``lock`` atomically and return the path."""
    lock_path = Path(path) if path is not None else default_lock_path()
    text = json.dumps(lock.payload(), indent=2, ensure_ascii=False) + "\n"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_name(lock_path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, lock_path)
    return lock_path


def _parse_lock(raw: Any, path: Path) -> PluginLock:
    if not isinstance(raw, dict):
        raise PluginLifecycleError(f"plugin lock {path} must be a JSON object")
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise PluginLifecycleError(f"plugin lock {path} has unknown keys: {unknown}")
    missing = sorted(_TOP_LEVEL_KEYS - set(raw))
    if missing:
        raise PluginLifecycleError(f"plugin lock {path} is missing required keys: {missing}")

    schema_version = _integer(raw["schemaVersion"], path, "schemaVersion")
    if schema_version != SCHEMA_VERSION:
        raise PluginLifecycleError(f"plugin lock {path}: unsupported schemaVersion {schema_version!r}")
    api_version = _integer(raw["apiVersion"], path, "apiVersion")
    if api_version != PLUGIN_API_VERSION:
        raise PluginLifecycleError(
            f"plugin lock {path}: unsupported apiVersion {api_version!r}; expected {PLUGIN_API_VERSION}"
        )

    raw_plugins = raw["plugins"]
    if not isinstance(raw_plugins, list):
        raise PluginLifecycleError(f"plugin lock {path}: plugins must be a list")

    plugins: list[LockedPlugin] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_plugins):
        plugin = _parse_locked_plugin(item, path, index)
        if plugin.key in seen:
            raise PluginLifecycleError(
                f"plugin lock {path}: duplicate plugin entry {plugin.id!r} version {plugin.version!r}"
            )
        seen.add(plugin.key)
        plugins.append(plugin)

    return PluginLock(plugins=tuple(plugins), schema_version=schema_version, api_version=api_version)


def _parse_locked_plugin(raw: Any, path: Path, index: int) -> LockedPlugin:
    label = f"plugin lock {path}: plugins[{index}]"
    if not isinstance(raw, dict):
        raise PluginLifecycleError(f"{label} must be an object")
    unknown = sorted(set(raw) - _PLUGIN_KEYS)
    if unknown:
        raise PluginLifecycleError(f"{label} has unknown keys: {unknown}")
    missing = sorted(_PLUGIN_KEYS - set(raw))
    if missing:
        raise PluginLifecycleError(f"{label} is missing required keys: {missing}")

    plugin_id = _string(raw["id"], label, "id")
    version = _string(raw["version"], label, "version")
    api = _integer(raw["api"], label, "api")
    if api != PLUGIN_API_VERSION:
        raise PluginLifecycleError(f"{label}: unsupported api {api!r}; expected {PLUGIN_API_VERSION}")
    entrypoint = _string(raw["entrypoint"], label, "entrypoint")
    parse_entrypoint(entrypoint)
    manifest_sha256 = _string(raw["manifestSha256"], label, "manifestSha256")
    if not _SHA256.match(manifest_sha256):
        raise PluginLifecycleError(f"{label}: manifestSha256 must be a lowercase SHA-256 hex digest")

    raw_variants = raw["variants"]
    if not isinstance(raw_variants, dict):
        raise PluginLifecycleError(f"{label}: variants must be an object")
    variants: dict[str, LockedVariant] = {}
    for variant_id, value in raw_variants.items():
        variant_label = f"{label}.variants[{variant_id!r}]"
        if not isinstance(variant_id, str) or not variant_id.strip():
            raise PluginLifecycleError(f"{label}: variant ids must be non-empty strings")
        if not isinstance(value, dict):
            raise PluginLifecycleError(f"{variant_label} must be an object")
        variant_unknown = sorted(set(value) - _VARIANT_KEYS)
        if variant_unknown:
            raise PluginLifecycleError(f"{variant_label} has unknown keys: {variant_unknown}")
        variant_missing = sorted(_VARIANT_KEYS - set(value))
        if variant_missing:
            raise PluginLifecycleError(f"{variant_label} is missing required keys: {variant_missing}")
        variant_version = _string(value["version"], variant_label, "version")
        implementation_hash = _string(value["implementationHash"], variant_label, "implementationHash")
        if not _SHA256.match(implementation_hash):
            raise PluginLifecycleError(f"{variant_label}: implementationHash must be a lowercase SHA-256 hex digest")
        variants[variant_id] = LockedVariant(
            version=variant_version,
            implementation_hash=implementation_hash,
        )

    return LockedPlugin(
        id=plugin_id,
        version=version,
        api=api,
        entrypoint=entrypoint,
        manifest_sha256=manifest_sha256,
        variants=variants,
    )


def _string(value: object, label: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginLifecycleError(f"{label}: {field} must be a non-empty string")
    return value


def _integer(value: object, label: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginLifecycleError(f"{label}: {field} must be an integer")
    return value
