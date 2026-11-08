"""Cross-layer execution registry and dispatch entry point.

The plugin lock owns installable process plugins.  Built-in geometry operations
are core specs, so they are added after the manifest+lock registry is loaded.
This module is intentionally outside :mod:`process_models.v2` to avoid a
cycle: the v2 package never imports the plugin loader.
"""

from __future__ import annotations

from typing import Any, Mapping

from process_models.v2.dispatch import DispatchDecision, dispatch_step
from process_models.v2.geometry import register_geometry
from process_models.v2.registry import ProcessRegistry

__all__ = ["load_execution_registry", "resolve_dispatch"]


def load_execution_registry(plugin_dirs: Any = None, lock_path: Any = None) -> ProcessRegistry:
    """Load the locked plugins and add the built-in geometry operation specs."""
    from process_models.plugins import load_registry

    registry = load_registry(plugin_dirs, lock_path)
    register_geometry(registry)
    return registry


def resolve_dispatch(
    project: Mapping[str, Any],
    step: Mapping[str, Any],
    *,
    registry: ProcessRegistry | None = None,
) -> DispatchDecision:
    """Resolve one step against a fresh or caller-supplied execution registry."""
    if registry is None:
        registry = load_execution_registry()
    return dispatch_step(registry, project, step)
