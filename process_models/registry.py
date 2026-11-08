"""Explicit registry for process-model plugins and their engine variants.

Projects reference stable IDs only (``plugin_id`` / ``variant_id``). This
module is the single place that turns those IDs into an implementation and a
content hash. It never imports a module named by project JSON.

Version selection
-----------------
Multiple versions of the same plugin id may be registered at once. When no
version is pinned, the registry prefers the highest semantic version
(``MAJOR.MINOR.PATCH``). If any registered version is not parseable as
semver, it falls back to registration order, which keeps behaviour
deterministic for commit-style versions such as ``"rev-a"``.

Non-goals
---------
No discovery, no entry points, no caching of native objects, and no
enforcement of :class:`VariantCapabilities`. The registry is intentionally a
lookup table with identity checks.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import dataclass

from pydantic import BaseModel

from .contract import EngineVariant, ModelDescriptor, ProcessPlugin

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class ResolvedVariant:
    """A concrete variant plus the identity that produced it.

    ``implementation_hash`` is either the variant's declared hash or the
    SHA-256 of its class source, so two runs of the same code resolve to the
    same value. It is suitable for run records and cache keys.
    """

    plugin_id: str
    plugin_version: str
    variant: EngineVariant
    variant_id: str
    variant_version: str
    implementation_hash: str


@dataclass(frozen=True)
class _Registration:
    plugin: ProcessPlugin
    descriptor: ModelDescriptor


class ProcessModelRegistry:
    """In-memory registry of plugins, keyed by ``(plugin_id, version)``.

    Registration order is preserved and used as the tie-breaker for
    non-semver versions and for :meth:`list_descriptors` output order.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, _Registration]] = {}

    def register(self, plugin: ProcessPlugin) -> None:
        """Register one plugin version and all of its variants.

        Raises ``TypeError`` for a malformed plugin or params model, and
        ``ValueError`` when the plugin identity, descriptor, or variant set is
        inconsistent or already registered. A given ``(plugin_id, version)``
        may be registered only once, which also guarantees that duplicate
        ``(plugin_id, plugin_version, variant_id)`` triples are rejected.
        """
        plugin_id = _identifier(getattr(plugin, "id", None), "plugin.id")
        plugin_version = _identifier(getattr(plugin, "version", None), "plugin.version")

        params_model = getattr(plugin, "params_model", None)
        if not (isinstance(params_model, type) and issubclass(params_model, BaseModel)):
            raise TypeError(f"{plugin_id} {plugin_version}: params_model must be a pydantic BaseModel subclass")

        variants = list(getattr(plugin, "variants", ()) or ())
        if not variants:
            raise ValueError(f"{plugin_id} {plugin_version}: at least one engine variant is required")

        seen: set[str] = set()
        for variant in variants:
            if not isinstance(variant, EngineVariant):
                raise TypeError(f"{plugin_id} {plugin_version}: variants must be EngineVariant instances")
            variant_id = _identifier(getattr(variant, "variant_id", None), f"{plugin_id}.variant_id")
            _identifier(getattr(variant, "version", None), f"{plugin_id}.{variant_id}.version")
            if variant_id in seen:
                raise ValueError(f"{plugin_id} {plugin_version}: duplicate variant id {variant_id!r}")
            seen.add(variant_id)

        descriptor = plugin.describe()
        if not isinstance(descriptor, ModelDescriptor):
            raise TypeError(f"{plugin_id} {plugin_version}: describe() must return a ModelDescriptor")
        if descriptor.id != plugin_id:
            raise ValueError(f"{plugin_id} {plugin_version}: descriptor id {descriptor.id!r} does not match plugin id")
        if descriptor.version != plugin_version:
            raise ValueError(
                f"{plugin_id} {plugin_version}: descriptor version {descriptor.version!r} does not match plugin version"
            )

        versions = self._plugins.setdefault(plugin_id, {})
        if plugin_version in versions:
            raise ValueError(f"process model {plugin_id!r} version {plugin_version!r} is already registered")
        versions[plugin_version] = _Registration(plugin=plugin, descriptor=descriptor)

    def get(self, plugin_id: str) -> ProcessPlugin:
        """Return the newest registered version of ``plugin_id``.

        Raises ``KeyError`` for an unknown plugin id.
        """
        versions = self._versions(plugin_id)
        return versions[_latest_version(list(versions))].plugin

    def resolve(
        self,
        plugin_id: str,
        variant_id: str,
        expected_plugin_version: str | None = None,
        expected_variant_version: str | None = None,
    ) -> ResolvedVariant:
        """Resolve stable IDs to a concrete variant.

        Without ``expected_plugin_version`` the newest plugin version that
        provides ``variant_id`` is used. Without ``expected_variant_version``
        any variant version is accepted. Pins are strict: a missing pinned
        plugin or variant version raises ``KeyError`` rather than falling
        back, so a saved run can detect drift.
        """
        versions = self._versions(plugin_id)

        if expected_plugin_version is not None:
            if expected_plugin_version not in versions:
                raise KeyError(f"unknown version {expected_plugin_version!r} for process model plugin {plugin_id!r}")
            candidates = [expected_plugin_version]
        else:
            candidates = _versions_newest_first(list(versions))

        for plugin_version in candidates:
            plugin = versions[plugin_version].plugin
            for variant in plugin.variants:
                if variant.variant_id != variant_id:
                    continue
                if expected_variant_version is not None and variant.version != expected_variant_version:
                    continue
                return ResolvedVariant(
                    plugin_id=plugin_id,
                    plugin_version=plugin_version,
                    variant=variant,
                    variant_id=variant.variant_id,
                    variant_version=variant.version,
                    implementation_hash=_implementation_hash(variant),
                )

        detail = f"variant {variant_id!r}"
        if expected_variant_version is not None:
            detail += f" version {expected_variant_version!r}"
        if expected_plugin_version is not None:
            detail += f" for plugin version {expected_plugin_version!r}"
        raise KeyError(f"unknown {detail} for process model plugin {plugin_id!r}")

    def list_descriptors(self) -> list[ModelDescriptor]:
        """Return the newest descriptor per plugin id, in registration order."""
        return [
            plugin_versions[_latest_version(list(plugin_versions))].descriptor
            for plugin_versions in self._plugins.values()
        ]

    def _versions(self, plugin_id: str) -> dict[str, _Registration]:
        versions = self._plugins.get(plugin_id)
        if not versions:
            raise KeyError(f"unknown process model plugin {plugin_id!r}")
        return versions


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _latest_version(versions: list[str]) -> str:
    """Pick the highest semver, or the last registered version otherwise."""
    keys = [_semver_key(version) for version in versions]
    if all(key is not None for key in keys):
        return max(zip(keys, versions), key=lambda item: item[0])[1]  # type: ignore[arg-type]
    return versions[-1]


def _versions_newest_first(versions: list[str]) -> list[str]:
    keys = [_semver_key(version) for version in versions]
    if all(key is not None for key in keys):
        return sorted(versions, key=_semver_key, reverse=True)  # type: ignore[arg-type]
    return list(reversed(versions))


def _implementation_hash(variant: EngineVariant) -> str:
    """Return the variant's declared hash, or a SHA-256 of its class source.

    Source hashing is stable for code that lives in a file. For classes
    created dynamically (where ``inspect.getsource`` is unavailable) the
    fallback hashes the fully qualified class name instead.
    """
    declared = getattr(variant, "implementation_hash", None)
    if isinstance(declared, str) and declared.strip():
        return declared
    try:
        source = inspect.getsource(type(variant))
    except (OSError, TypeError):
        source = f"{type(variant).__module__}.{type(variant).__qualname__}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
