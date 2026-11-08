"""Installable process-model plugins.

Each subdirectory is one plugin package with a ``plugin.toml`` manifest and
the implementation next to it. The platform discovers manifests from
operator-controlled directories; project JSON never names this package or a
module path.

The ``plugins`` package exists so in-tree plugins can also be imported as
``plugins.<id>`` during tests. Installed external packages may instead use a
top-level module name declared in their manifest entrypoint.
"""
