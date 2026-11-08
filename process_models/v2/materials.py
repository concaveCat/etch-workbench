"""Engine-neutral material-name registry.

Native material IDs are engine-specific. Plugins and the platform speak material
*names*; the engine's :class:`MaterialRegistry` maps those names to the IDs the
native handle uses. Keeping this boundary explicit prevents every plugin from
hard-coding ``getattr(ps.Material, name)`` or assuming a native ID is portable.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

__all__ = [
    "MaterialRegistry",
    "MaterialRegistryError",
    "StaticMaterialRegistry",
    "collect_material_names",
    "missing_material_names",
]


class MaterialRegistryError(RuntimeError):
    """Raised when a material name or ID is absent or inconsistent."""


#: Parameter keys whose values name native materials. Only scalar strings and
#: lists of strings are collected; dictionaries such as ``materialRates`` are
#: rate tables, not material-name fields.
_MATERIAL_KEYS = frozenset(
    {
        "material",
        "maskMaterial",
        "maskMaterials",
        "screenMaterials",
        "diffusionMaterials",
        "blockingMaterials",
        "depositMaterial",
        "siliconMaterial",
        "oxideMaterial",
    }
)


def collect_material_names(params: Any) -> tuple[str, ...]:
    """Return every material name carried by a typed params object.

    The walk is deliberately declarative: a plugin may nest parameters, but a
    field named ``material``/``*Materials`` is a native material reference.
    Empty strings (for example oxidation's no-mask default) are ignored.
    """
    if hasattr(params, "model_dump"):
        data = params.model_dump(mode="python")
    else:
        data = params

    names: list[str] = []

    def walk(value: Any, key: str | None = None) -> None:
        if key in _MATERIAL_KEYS:
            if isinstance(value, str):
                if value.strip():
                    names.append(value)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        names.append(item)
                return
        if isinstance(value, MappingABC):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    walk(data)
    return tuple(dict.fromkeys(names))


def missing_material_names(params: Any, registry: MaterialRegistry) -> tuple[str, ...]:
    """Return material names in ``params`` that ``registry`` cannot resolve."""
    missing: list[str] = []
    for name in collect_material_names(params):
        try:
            registry.id_for(name)
        except MaterialRegistryError:
            missing.append(name)
    return tuple(dict.fromkeys(missing))


@runtime_checkable
class MaterialRegistry(Protocol):
    """Name <-> native material-id mapping for one engine."""

    def id_for(self, name: str) -> int: ...

    def name_for(self, material_id: int) -> str | None: ...

    def require(self, names: Iterable[str]) -> tuple[int, ...]: ...


@dataclass(frozen=True)
class StaticMaterialRegistry:
    """A small immutable registry for tests and custom engines."""

    mapping: Mapping[str, int]

    def __post_init__(self) -> None:
        mapping = {str(name): int(material_id) for name, material_id in dict(self.mapping).items()}
        if any(not name.strip() for name in mapping):
            raise ValueError("material names must be non-empty strings")
        if any(material_id < 0 for material_id in mapping.values()):
            raise ValueError(f"material ids must be >= 0, got {sorted(mapping.values())!r}")
        identifiers = list(mapping.values())
        duplicates = sorted({material_id for material_id in identifiers if identifiers.count(material_id) > 1})
        if duplicates:
            raise ValueError(f"material ids must be unique, got duplicates {duplicates!r}")
        object.__setattr__(self, "mapping", mapping)

    def id_for(self, name: str) -> int:
        try:
            return self.mapping[str(name)]
        except KeyError as error:
            raise MaterialRegistryError(f"material {name!r} is not registered") from error

    def name_for(self, material_id: int) -> str | None:
        for name, candidate in self.mapping.items():
            if candidate == int(material_id):
                return name
        return None

    def require(self, names: Iterable[str]) -> tuple[int, ...]:
        return tuple(self.id_for(name) for name in names)
