"""Native SF6/C4F8 plasma-etch plugin.

This package is an installable plugin: ``plugin.toml`` sits next to the code,
and the manifest entrypoint is ``plugins.sf6c4f8:register``. The platform
loader adds the plugin directory's parent to ``sys.path``, so an external
operator package can use the same ``module:attribute`` shape.

Importing this package must not import the worker or :mod:`backend`; the
native module is supplied at execute time through the engine lease.
"""

from __future__ import annotations

from .params import SF6C4F8Params
from .register import register
from .spec import SF6C4F8ProcessSpec
from .step import parse_step
from .variants import SF6C4F8NativeVariant

__all__ = [
    "SF6C4F8NativeVariant",
    "SF6C4F8Params",
    "SF6C4F8ProcessSpec",
    "parse_step",
    "register",
]
