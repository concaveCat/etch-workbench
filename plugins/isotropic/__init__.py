"""Native isotropic etch/deposit plugin.

This package is an installable plugin: ``plugin.toml`` sits next to the code,
and the manifest entrypoint is ``plugins.isotropic:register``. The platform
loader adds the plugin directory's parent to ``sys.path``, so an external
operator package can use the same ``module:attribute`` shape.
"""

from __future__ import annotations

from .params import IsotropicParams
from .register import register
from .spec import IsotropicProcessSpec
from .step import parse_step
from .variants import IsotropicNativeVariant

__all__ = [
    "IsotropicNativeVariant",
    "IsotropicParams",
    "IsotropicProcessSpec",
    "parse_step",
    "register",
]
