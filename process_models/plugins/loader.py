"""Discover plugin manifests, verify the lock, and build the registry.

The loader is the only code that turns a manifest entrypoint into an import.
It scans operator-controlled directories, imports exactly the declared
``module:attribute`` (never a path from project JSON), calls
``register(registry)``, and checks the resulting process/variant descriptors
against the manifest identity and the checked-in lock.

Any mismatch raises :class:`PluginLifecycleError`; there is no fallback.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from process_models.v2.contract import ProcessSpec, VariantRegistration
from process_models.v2.registry import ProcessRegistry, compute_implementation_hash

from .errors import PluginLifecycleError
from .lock import LockedPlugin, LockedVariant, PluginLock, read_lock, write_lock_file
from .manifest import (
    MANIFEST_FILENAME,
    REPO_ROOT,
    PluginManifest,
    default_plugin_dirs,
    parse_entrypoint,
    read_manifest,
    resolve_path,
)

__all__ = [
    "DiscoveredPlugin",
    "default_plugin_dirs",
    "discover_manifests",
    "load_registry",
    "verify_lock",
    "write_lock",
]


@dataclass(frozen=True)
class DiscoveredPlugin:
    """A manifest plus the location it was found at."""

    manifest: PluginManifest
    manifest_path: Path
    plugin_dir: Path

    @property
    def key(self) -> tuple[str, str]:
        return self.manifest.key


def discover_manifests(plugin_dirs: object = None) -> list[DiscoveredPlugin]:
    """Scan ``dir/*/plugin.toml`` under each plugin directory.

    ``plugin_dirs`` may be ``None`` (use :func:`default_plugin_dirs`), a single
    path, or an iterable of paths. Relative paths resolve against the repo
    root. Manifests are parsed strictly while scanning.
    """
    dirs = default_plugin_dirs() if plugin_dirs is None else _coerce_dirs(plugin_dirs)
    discovered: list[DiscoveredPlugin] = []
    for base in dirs:
        if not base.is_dir():
            continue
        for manifest_path in sorted(base.glob(f"*/{MANIFEST_FILENAME}")):
            if not manifest_path.is_file():
                continue
            discovered.append(
                DiscoveredPlugin(
                    manifest=read_manifest(manifest_path),
                    manifest_path=manifest_path,
                    plugin_dir=manifest_path.parent,
                )
            )
    return discovered


def write_lock(plugin_dirs: object = None, lock_path: str | os.PathLike[str] | None = None) -> Path:
    """Discover plugins, verify their registrations, and write the lock.

    The implementation hash is always
    :func:`process_models.v2.registry.compute_implementation_hash`; a variant's
    self-declared hash is not trusted for the lock.
    """
    records = discover_manifests(plugin_dirs)
    _check_unique_identities(records)

    entries: list[LockedPlugin] = []
    registries: list[ProcessRegistry] = []
    for record in sorted(records, key=lambda item: item.key):
        variants, registry = _describe_registration(record)
        registries.append(registry)
        entries.append(
            LockedPlugin(
                id=record.manifest.id,
                version=record.manifest.version,
                api=record.manifest.api,
                entrypoint=record.manifest.entrypoint,
                manifest_sha256=_manifest_sha256(record.manifest_path),
                variants=variants,
            )
        )

    # Build the combined registry as well: a lock that cannot be loaded is not
    # a valid lock.
    _merge_registries(registries)
    return write_lock_file(PluginLock(plugins=tuple(entries)), lock_path)


def verify_lock(plugin_dirs: object = None, lock_path: str | os.PathLike[str] | None = None) -> None:
    """Verify manifests, lock entries, and implementation hashes.

    Raises :class:`PluginLifecycleError` on any mismatch, missing manifest or
    lock entry, duplicate identity, or failed registration.
    """
    _load_verified(plugin_dirs, lock_path)


def load_registry(plugin_dirs: object = None, lock_path: str | os.PathLike[str] | None = None) -> ProcessRegistry:
    """Verify the lock and return the combined :class:`ProcessRegistry`.

    The returned registry contains exactly the plugins declared by manifests
    and accepted by the lock. Callers resolve stable IDs only.
    """
    return _load_verified(plugin_dirs, lock_path)


def _load_verified(plugin_dirs: object, lock_path: str | os.PathLike[str] | None) -> ProcessRegistry:
    lock = read_lock(lock_path)
    records = discover_manifests(plugin_dirs)
    _check_unique_identities(records)

    record_map = {record.key: record for record in records}
    locked_map = lock.entries()

    extra = sorted(set(locked_map) - set(record_map))
    if extra:
        raise PluginLifecycleError(
            "plugin lock has entries with no matching manifest: "
            + ", ".join(f"{plugin_id}@{version}" for plugin_id, version in extra)
        )
    missing = sorted(set(record_map) - set(locked_map))
    if missing:
        raise PluginLifecycleError(
            "plugin manifests have no matching lock entry: "
            + ", ".join(f"{plugin_id}@{version}" for plugin_id, version in missing)
        )

    registries: list[ProcessRegistry] = []
    for key in sorted(record_map):
        record = record_map[key]
        variants, registry = _describe_registration(record)
        _compare_with_lock(record, locked_map[key], variants)
        registries.append(registry)

    return _merge_registries(registries)


def _coerce_dirs(plugin_dirs: object) -> tuple[Path, ...]:
    if isinstance(plugin_dirs, (str, os.PathLike)):
        values = (plugin_dirs,)
    else:
        try:
            values = tuple(plugin_dirs)  # type: ignore[arg-type]
        except TypeError as error:
            raise PluginLifecycleError(
                f"plugin_dirs must be a path or an iterable of paths, got {plugin_dirs!r}"
            ) from error
    if not values:
        raise PluginLifecycleError("plugin_dirs must not be empty")
    return tuple(resolve_path(value) for value in values)


def _check_unique_identities(records: list[DiscoveredPlugin]) -> None:
    seen: dict[tuple[str, str], Path] = {}
    for record in records:
        key = record.key
        if key in seen:
            raise PluginLifecycleError(
                f"duplicate plugin {record.manifest.id!r} version {record.manifest.version!r}: "
                f"{seen[key]} and {record.manifest_path}"
            )
        seen[key] = record.manifest_path


def _ensure_import_path(plugin_dir: Path) -> None:
    for entry in (plugin_dir.parent, REPO_ROOT):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def _load_entrypoint(record: DiscoveredPlugin):
    manifest = record.manifest
    module_name, attribute = parse_entrypoint(manifest.entrypoint)
    _ensure_import_path(record.plugin_dir)

    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise PluginLifecycleError(
            f"plugin {manifest.id!r} version {manifest.version!r}: cannot import entrypoint module "
            f"{module_name!r}: {error}"
        ) from error

    try:
        register = getattr(module, attribute)
    except AttributeError as error:
        raise PluginLifecycleError(
            f"plugin {manifest.id!r} version {manifest.version!r}: module {module_name!r} "
            f"has no attribute {attribute!r}"
        ) from error
    if not callable(register):
        raise PluginLifecycleError(
            f"plugin {manifest.id!r} version {manifest.version!r}: entrypoint {manifest.entrypoint!r} is not callable"
        )
    return register


def _register(record: DiscoveredPlugin) -> ProcessRegistry:
    manifest = record.manifest
    register = _load_entrypoint(record)
    registry = ProcessRegistry()
    try:
        register(registry)
    except PluginLifecycleError:
        raise
    except Exception as error:
        raise PluginLifecycleError(
            f"plugin {manifest.id!r} version {manifest.version!r}: entrypoint {manifest.entrypoint!r} failed: {error}"
        ) from error
    return registry


def _describe_registration(record: DiscoveredPlugin) -> tuple[dict[str, LockedVariant], ProcessRegistry]:
    manifest = record.manifest
    registry = _register(record)

    actual_processes = {process_id: set(versions) for process_id, versions in _registry_processes(registry).items()}
    expected_processes = {manifest.id: {manifest.version}}
    if actual_processes != expected_processes:
        raise PluginLifecycleError(
            f"plugin {manifest.id!r} version {manifest.version!r}: entrypoint registered "
            f"{_format_processes(actual_processes)}; expected {_format_processes(expected_processes)}"
        )

    variants: dict[str, LockedVariant] = {}
    for registration in _all_variant_registrations(registry, manifest.id):
        variant_id = registration.variant_id
        if variant_id in variants:
            raise PluginLifecycleError(
                f"plugin {manifest.id!r} version {manifest.version!r}: variant {variant_id!r} "
                "is registered more than once"
            )
        variants[variant_id] = LockedVariant(
            version=registration.version,
            implementation_hash=compute_implementation_hash(registration.variant),
        )
    return variants, registry


def _compare_with_lock(
    record: DiscoveredPlugin,
    locked: LockedPlugin,
    variants: dict[str, LockedVariant],
) -> None:
    manifest = record.manifest
    label = f"plugin {manifest.id!r} version {manifest.version!r}"
    if locked.api != manifest.api:
        raise PluginLifecycleError(f"{label}: lock api {locked.api!r} does not match manifest api {manifest.api!r}")
    if locked.entrypoint != manifest.entrypoint:
        raise PluginLifecycleError(
            f"{label}: lock entrypoint {locked.entrypoint!r} does not match manifest entrypoint {manifest.entrypoint!r}"
        )

    manifest_sha256 = _manifest_sha256(record.manifest_path)
    if locked.manifest_sha256 != manifest_sha256:
        raise PluginLifecycleError(
            f"{label}: manifest bytes changed since the lock was written "
            f"(lock {locked.manifest_sha256}, actual {manifest_sha256})"
        )

    locked_ids = set(locked.variants)
    actual_ids = set(variants)
    if locked_ids != actual_ids:
        missing = sorted(locked_ids - actual_ids)
        extra = sorted(actual_ids - locked_ids)
        raise PluginLifecycleError(f"{label}: variant set differs from the lock (missing={missing}, extra={extra})")

    for variant_id, locked_variant in locked.variants.items():
        actual = variants[variant_id]
        if locked_variant.version != actual.version:
            raise PluginLifecycleError(
                f"{label}: variant {variant_id!r} version {actual.version!r} does not match "
                f"lock version {locked_variant.version!r}"
            )
        if locked_variant.implementation_hash != actual.implementation_hash:
            raise PluginLifecycleError(
                f"{label}: variant {variant_id!r} implementation changed since the lock was written "
                f"(lock {locked_variant.implementation_hash}, actual {actual.implementation_hash})"
            )


def _merge_registries(registries: list[ProcessRegistry]) -> ProcessRegistry:
    """Combine verified registries, de-duplicating identical variant identities.

    The v2 registry keys variants by ``(process_id, variant_id, version)`` and
    does not tie them to a process version. Two manifest versions may therefore
    declare the same variant; that is accepted only when the implementation
    hash is identical.
    """
    combined = ProcessRegistry()
    for registry in registries:
        for versions in _registry_processes(registry).values():
            for spec in versions.values():
                combined.register_process(spec)

        for process_id, by_variant in _registry_variants(registry).items():
            for variant_id, registrations in by_variant.items():
                for registration in registrations:
                    existing = _find_variant(combined, process_id, variant_id, registration.version)
                    if existing is None:
                        combined.register_variant(registration)
                        continue
                    if compute_implementation_hash(existing.variant) != compute_implementation_hash(
                        registration.variant
                    ):
                        raise PluginLifecycleError(
                            f"plugin {process_id!r}: variant {variant_id!r} version "
                            f"{registration.version!r} has conflicting implementations across plugin versions"
                        )
    return combined


def _registry_processes(registry: ProcessRegistry) -> dict[str, dict[str, ProcessSpec]]:
    processes = getattr(registry, "_processes", None)
    if not isinstance(processes, dict):
        raise PluginLifecycleError("unsupported ProcessRegistry: no process index available for verification")
    return processes


def _registry_variants(registry: ProcessRegistry) -> dict[str, dict[str, list[VariantRegistration]]]:
    variants = getattr(registry, "_variants", None)
    if not isinstance(variants, dict):
        raise PluginLifecycleError("unsupported ProcessRegistry: no variant index available for verification")
    return variants


def _all_variant_registrations(registry: ProcessRegistry, process_id: str) -> list[VariantRegistration]:
    return [
        registration
        for registrations in _registry_variants(registry).get(process_id, {}).values()
        for registration in registrations
    ]


def _find_variant(
    registry: ProcessRegistry,
    process_id: str,
    variant_id: str,
    version: str,
) -> VariantRegistration | None:
    for registration in _registry_variants(registry).get(process_id, {}).get(variant_id, []):
        if registration.version == version:
            return registration
    return None


def _manifest_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PluginLifecycleError(f"cannot read plugin manifest {path}: {error}") from error


def _format_processes(processes: dict[str, set[str]]) -> dict[str, list[str]]:
    return {process_id: sorted(versions) for process_id, versions in sorted(processes.items())}
