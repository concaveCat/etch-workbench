"""Native fluorocarbon plasma-etch plugin.

This package is an installable plugin: ``plugin.toml`` sits next to the code,
and the manifest entrypoint is ``plugins.fluorocarbon:register``. The platform
loader adds the plugin directory's parent to ``sys.path``, so an external
operator package can use the same ``module:attribute`` shape.

Importing this package must not import the worker or :mod:`backend`; the
native module is supplied at execute time through the engine lease.
"""

from __future__ import annotations

from .params import FluorocarbonParams
from .register import register
from .spec import FluorocarbonProcessSpec
from .step import parse_step
from .variants import FluorocarbonNativeVariant

__all__ = [
    "FluorocarbonNativeVariant",
    "FluorocarbonParams",
    "FluorocarbonProcessSpec",
    "parse_step",
    "register",
]
