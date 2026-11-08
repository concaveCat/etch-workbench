"""Errors raised by the plugin lifecycle.

Every lifecycle failure uses one exception type so callers can distinguish a
plugin/lock problem from an ordinary import or registration error. There is
no fallback path: a missing or stale lock, a tampered manifest, or a
registration outside the manifest's identity all raise
:class:`PluginLifecycleError`.
"""

from __future__ import annotations

__all__ = ["PluginLifecycleError"]


class PluginLifecycleError(RuntimeError):
    """A plugin manifest, lock, or registration failed verification."""
