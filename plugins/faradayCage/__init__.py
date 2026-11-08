"""Native Faraday-cage ion-beam-etch plugin.

This package is an installable plugin: ``plugin.toml`` sits next to the code,
and the manifest entrypoint is ``plugins.faradayCage:register``. The platform
loader adds the plugin directory's parent to ``sys.path``, so an external
operator package can use the same ``module:attribute`` shape.

Importing this package must not import the worker or :mod:`backend`; the
native module is supplied at execute time through the engine lease.
"""

from __future__ import annotations

from .params import FaradayCageParams
from .register import register
from .spec import FaradayCageProcessSpec
from .step import parse_step
from .variants import FaradayCageNativeVariant

__all__ = [
    "FaradayCageNativeVariant",
    "FaradayCageParams",
    "FaradayCageProcessSpec",
    "parse_step",
    "register",
]
