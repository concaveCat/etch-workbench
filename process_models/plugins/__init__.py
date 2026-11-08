"""Installable process-model plugins: manifest, discovery, lock, and loader.

Public surface:

* :data:`PLUGIN_API_VERSION` - manifest/lock API version understood here.
* :class:`PluginManifest` / :func:`read_manifest` - strict ``plugin.toml``.
* :func:`default_plugin_dirs` / :func:`discover_manifests` - operator-scanned
  plugin directories (``dir/*/plugin.toml``).
* :func:`write_lock` / :func:`verify_lock` - generate and verify
  ``plugins.lock``.
* :func:`load_registry` - verify the lock and return a
  :class:`~process_models.v2.registry.ProcessRegistry`.

Projects reference stable process/variant IDs only. Module paths and
callables live exclusively in manifests and are resolved by the loader.
"""

from __future__ import annotations

from .errors import PluginLifecycleError
from .loader import DiscoveredPlugin, discover_manifests, load_registry, verify_lock, write_lock
from .lock import LOCK_FILENAME, SCHEMA_VERSION, LockedPlugin, LockedVariant, PluginLock, default_lock_path, read_lock
from .manifest import PLUGIN_API_VERSION, PluginManifest, default_plugin_dirs, read_manifest

__all__ = [
    "LOCK_FILENAME",
    "PLUGIN_API_VERSION",
    "SCHEMA_VERSION",
    "DiscoveredPlugin",
    "LockedPlugin",
    "LockedVariant",
    "PluginLifecycleError",
    "PluginLock",
    "PluginManifest",
    "default_lock_path",
    "default_plugin_dirs",
    "discover_manifests",
    "load_registry",
    "read_lock",
    "read_manifest",
    "verify_lock",
    "write_lock",
]
