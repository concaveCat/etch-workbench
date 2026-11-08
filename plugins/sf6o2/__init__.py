"""Native SF6/O2 plasma-etch plugin.

This package is an installable plugin: ``plugin.toml`` sits next to the code,
and the manifest entrypoint is ``plugins.sf6o2:register``. The platform loader
adds the plugin directory's parent to ``sys.path``, so an external operator
package can use the same ``module:attribute`` shape.

Importing this package must not import the worker or :mod:`backend`; the
native module is supplied at execute time through the engine lease.
"""

from __future__ import annotations

from .params import SF6O2Params
from .register import register
from .spec import SF6O2ProcessSpec
from .step import parse_step
from .variants import SF6O2NativeVariant

__all__ = [
    "SF6O2NativeVariant",
    "SF6O2Params",
    "SF6O2ProcessSpec",
    "parse_step",
    "register",
]
