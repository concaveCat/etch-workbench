"""Engine helper packages for v2 plugin variants.

Only the ViennaPS helpers live here so far. They are the small, pure-Python
glue that used to sit in :mod:`worker.simulate`: material lookup, the material
map, rate signing, mask derivation and advection parameters. The worker and the
installed plugins both import them, and this package never imports
:mod:`worker`.
"""

from __future__ import annotations

from .viennaps import (
    configure_advection,
    material_handle,
    material_map,
    protected_material_names,
    signed_rates,
)

__all__ = [
    "configure_advection",
    "material_handle",
    "material_map",
    "protected_material_names",
    "signed_rates",
]
