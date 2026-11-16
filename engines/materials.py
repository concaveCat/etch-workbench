"""Workbench materials that the Memory1 reference flow needs but 4.7.0 lacks.

ViennaPS ships the 15-material workbench catalogue plus a registry that accepts new
materials at runtime. Memory1 (the SPX reference flow) also uses Oxide2, SiOC,
BARC, CESL, SiON and TaN; registering them here keeps every layer distinct instead
of collapsing several SPX materials onto one workbench material, which would merge
layers and make the comparison meaningless.

Ids are assigned by the native registry (custom materials start at 177) and are not
part of any stored state, because the comparison layer never persists a domain.
"""

from __future__ import annotations

from typing import Any

# name -> (colour, MaterialCategory, density g/cm3 if known)
EXTRA_MATERIALS: dict[str, tuple[str, str, float | None]] = {
    "Oxide2": ("#26c6da", "Insulator", None),
    "SiOC": ("#8d6e63", "Insulator", None),
    "BARC": ("#e6e672", "Insulator", None),
    "CESL": ("#55aaff", "Insulator", None),
    "SiON": ("#4dd0e1", "Insulator", None),
    "TaN": ("#b0bec5", "Conductor", None),
}


def ensure_registered(ps: Any) -> list[str]:
    """Register the extra materials once per process; return the names added."""
    registry = ps.MaterialRegistry.instance()
    added: list[str] = []
    for name, (colour, category, density) in EXTRA_MATERIALS.items():
        if registry.findMaterial(name) is not None:
            continue
        material = registry.registerMaterial(name)
        info = registry.getInfo(material)
        info.color_hex = int(colour.lstrip("#"), 16)
        try:
            info.category = ps.MaterialCategory[category]
        except KeyError:
            pass
        if density is not None:
            info.density_gcm3 = float(density)
        added.append(name)
    return added
