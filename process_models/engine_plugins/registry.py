"""Engine plugin loading and provider composition.

The platform core only sees ``EngineProvider``.  ViennaPS and any custom
engine are both loaded from ``engine.toml`` and composed in registration order;
there is no native fast path that bypasses this module.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from process_models.v2.engine import EngineProvider, EngineUnavailableError

from .lock import EngineLockEntry, compute_engine_hash, read_engine_lock
from .manifest import EngineManifest, default_engine_dirs, read_engine_manifest

__all__ = [
    "CompositeEngineProvider",
    "EngineContext",
    "EnginePlugin",
    "EngineRegistry",
    "load_engine_registry",
]


@dataclass(frozen=True)
class EngineContext:
    """Run-scoped inputs supplied to every engine plugin.

    The context is deliberately engine-neutral.  A plugin imports its own
    native package; the platform does not pass a ViennaPS module or handle.
    """

    project: Mapping[str, Any]
    run_dir: Path
    selected_device: Mapping[str, Any] | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str | None = None


@runtime_checkable
class EnginePlugin(Protocol):
    """One installable engine implementation."""

    engine_id: str
    version: str

    def create_provider(self, context: EngineContext) -> EngineProvider: ...


@dataclass(frozen=True)
class EngineRegistry:
    """Loaded engine plugins; providers are built per run context."""

    plugins: tuple[EnginePlugin, ...]

    def provider(self, context: EngineContext) -> EngineProvider:
        return CompositeEngineProvider(tuple(plugin.create_provider(context) for plugin in self.plugins))


class CompositeEngineProvider:
    """Try providers in registration order, with strict no-fallback matching."""

    def __init__(self, providers: Iterable[EngineProvider]) -> None:
        self._providers = tuple(providers)
        if not self._providers:
            raise ValueError("at least one engine provider is required")

    def engines(self):
        capabilities = []
        for provider in self._providers:
            capabilities.extend(provider.engines())
        return capabilities

    def acquire(self, requirement, policy=None):
        failures: list[str] = []
        for provider in self._providers:
            try:
                return provider.acquire(requirement, policy)
            except EngineUnavailableError as error:
                failures.append(str(error))
        detail = "; ".join(failures) if failures else "no engine providers are registered"
        raise EngineUnavailableError(
            f"no engine satisfies {requirement.kind!r} {requirement.engine_id!r} "
            f"[{requirement.min_version!r}, {requirement.max_version!r}]: {detail}"
        )


def load_engine_registry(
    engine_dirs: object = None,
    lock_path: str | None = None,
) -> EngineRegistry:
    """Discover engine manifests, verify ``engines.lock``, and import plugins."""
    lock = read_engine_lock(lock_path)
    records = _discover(engine_dirs)
    locked = lock.by_key()
    present = {record.manifest.key: record for record in records}
    if set(present) != set(locked):
        missing = sorted(set(locked) - set(present))
        extra = sorted(set(present) - set(locked))
        raise ValueError(f"engine plugin set differs from lock (missing={missing}, extra={extra})")

    plugins: list[EnginePlugin] = []
    for key in sorted(present):
        record = present[key]
        plugin = _load_plugin(record.manifest, record.plugin_dir)
        if plugin.engine_id != record.manifest.engine_id or plugin.version != record.manifest.version:
            raise ValueError(
                f"engine plugin {key!r} declares {plugin.engine_id!r}@{plugin.version!r}, "
                f"expected {record.manifest.engine_id!r}@{record.manifest.version!r}"
            )
        manifest_hash, implementation_hash = compute_engine_hash(record.manifest_path)
        expected: EngineLockEntry = locked[key]
        if manifest_hash != expected.manifest_sha256:
            raise ValueError(f"engine plugin {key!r}: engine.toml changed since engines.lock was written")
        if implementation_hash != expected.implementation_hash:
            raise ValueError(f"engine plugin {key!r}: implementation changed since engines.lock was written")
        plugins.append(plugin)
    return EngineRegistry(tuple(plugins))


@dataclass(frozen=True)
class _Record:
    manifest: EngineManifest
    manifest_path: Path
    plugin_dir: Path


def _discover(engine_dirs: object) -> list[_Record]:
    dirs = tuple(default_engine_dirs()) if engine_dirs is None else _coerce_dirs(engine_dirs)
    records: list[_Record] = []
    for base in dirs:
        if not base.is_dir():
            continue
        for manifest_path in sorted(base.glob("*/engine.toml")):
            if manifest_path.is_file():
                manifest = read_engine_manifest(manifest_path)
                records.append(_Record(manifest, manifest_path, manifest_path.parent))
    return records


def _coerce_dirs(value: object) -> tuple[Path, ...]:
    if isinstance(value, (str, Path)):
        values = (value,)
    else:
        try:
            values = tuple(value)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError("engine_dirs must be a path or an iterable of paths") from error
    result = []
    for item in values:
        path = Path(item).expanduser()
        result.append(path if path.is_absolute() else (Path.cwd() / path))
    return tuple(result)


def _load_plugin(manifest: EngineManifest, plugin_dir: Path) -> EnginePlugin:
    module_name, attribute = manifest.entrypoint.split(":", 1)
    parent = str(plugin_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        module = importlib.import_module(module_name)
        plugin = getattr(module, attribute)
    except Exception as error:  # noqa: BLE001 - report the entrypoint failure verbatim
        raise ValueError(
            f"engine plugin {manifest.engine_id!r}@{manifest.version!r}: cannot load "
            f"entrypoint {manifest.entrypoint!r}: {error}"
        ) from error
    if not isinstance(plugin, EnginePlugin):
        raise TypeError(
            f"engine plugin {manifest.engine_id!r}@{manifest.version!r}: entrypoint "
            f"{manifest.entrypoint!r} must expose an EnginePlugin"
        )
    return plugin
