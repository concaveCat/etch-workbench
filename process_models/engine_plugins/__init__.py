"""Installable engine plugins.

An engine plugin is the unit that knows how to construct an
``EngineProvider`` for one engine implementation.  Process plugins only
declare ``EngineRequirement``; the platform loads engine plugins through
``engine.toml`` + ``engines.lock`` and composes their providers.  ViennaPS is
one of those plugins, not a privileged branch in the worker.
"""

from __future__ import annotations

from .lock import (
    ENGINE_LOCK_FILENAME,
    EngineLock,
    EngineLockEntry,
    compute_engine_hash,
    read_engine_lock,
    write_engine_lock,
)
from .manifest import (
    ENGINE_API_VERSION,
    EngineManifest,
    default_engine_dirs,
    read_engine_manifest,
)
from .registry import (
    CompositeEngineProvider,
    EngineContext,
    EnginePlugin,
    EngineRegistry,
    load_engine_registry,
)

__all__ = [
    "CompositeEngineProvider",
    "ENGINE_API_VERSION",
    "ENGINE_LOCK_FILENAME",
    "EngineContext",
    "EngineLock",
    "EngineLockEntry",
    "EngineManifest",
    "EnginePlugin",
    "EngineRegistry",
    "compute_engine_hash",
    "default_engine_dirs",
    "load_engine_registry",
    "read_engine_lock",
    "read_engine_manifest",
    "write_engine_lock",
]
