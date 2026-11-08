"""Cell-set field domain boundary for v2.

A cell set is an auxiliary field domain derived from the canonical level-set
geometry.  It is not a second primary state: ``ProcessState.geometry`` remains
the authoritative surface, and ``ProcessState.fields`` is produced on demand
for a contiguous ``implant -> anneal -> analysis`` sequence.

This module deliberately contains no native import.  The ViennaPS
implementation lives in :mod:`process_models.v2.engines.viennaps_cellset`; an
engine supplies it through :attr:`process_models.v2.engine.EngineLease.cellset_backend`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .contract import ProcessContext
from .state import CellSetState, LevelSetState

__all__ = [
    "CELLSET_CONVERSION",
    "CellSetBackend",
    "CellSetBackendError",
    "CellSetCapabilities",
    "CellSetConfig",
]

#: Conversion name a field variant declares in its ``StateProfile``.
CELLSET_CONVERSION = "fields<-geometry:dense-cellset-v1"


class CellSetBackendError(RuntimeError):
    """Raised when a native cell set cannot be derived, snapshotted, or reloaded."""


@dataclass(frozen=True)
class CellSetCapabilities:
    """What a cell-set backend advertises.

    ``conversions`` uses the same names as
    :class:`process_models.v2.contract.StateProfile`.  ``sparse`` is ``False``
    for the first implementation: the generic :class:`CellSetState` grid is a
    dense lattice, so a native cell set whose active-cell count differs from
    ``product(shape)`` is refused instead of silently reindexed.
    """

    conversions: frozenset[str] = frozenset({CELLSET_CONVERSION})
    sparse: bool = False

    def __post_init__(self) -> None:
        conversions = frozenset(str(value) for value in self.conversions)
        if any(not value.strip() for value in conversions):
            raise ValueError(f"cell-set conversions must be non-empty strings, got {conversions!r}")
        object.__setattr__(self, "conversions", conversions)


@dataclass(frozen=True)
class CellSetConfig:
    """The canonical native ``DenseCellSet`` construction settings.

    ``height`` is the absolute top coordinate passed to
    ``Domain.generateCellSet`` (matching the schema-7 worker semantics, where a
    surface at ``z=0`` makes it the height above the surface).  ``substrate_depth``
    is the depth of the widened level-set band used before generation.
    ``cover_material`` and ``periodic`` must be identical across one field
    sequence; route compilation normalizes ``height``/``substrate_depth`` to the
    sequence maximum and fails on a cover/periodic conflict.
    """

    height: float
    substrate_depth: float
    cover_material: str = "GAS"
    periodic: bool = False

    def __post_init__(self) -> None:
        height = float(self.height)
        substrate_depth = float(self.substrate_depth)
        if not math.isfinite(height) or height <= 0.0:
            raise ValueError(f"cellSet.height must be a positive finite number, got {self.height!r}")
        if not math.isfinite(substrate_depth) or substrate_depth <= 0.0:
            raise ValueError(f"cellSet.substrateDepth must be a positive finite number, got {self.substrate_depth!r}")
        cover_material = str(self.cover_material)
        if not cover_material.strip():
            raise ValueError("cellSet.coverMaterial must be a non-empty string")
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "substrate_depth", substrate_depth)
        object.__setattr__(self, "cover_material", cover_material)
        object.__setattr__(self, "periodic", bool(self.periodic))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CellSetConfig:
        """Build a config from a raw (camelCase) project ``cellSet`` object."""
        if not isinstance(payload, Mapping):
            raise CellSetBackendError(f"cellSet must be a JSON object, got {type(payload).__name__}")
        if payload.get("substrateDepth") is None:
            raise CellSetBackendError(
                "cellSet.substrateDepth is required: a narrow-band level set would otherwise "
                "produce a single substrate row"
            )
        try:
            return cls(
                height=payload["height"],
                substrate_depth=payload["substrateDepth"],
                cover_material=payload.get("coverMaterial") or "GAS",
                periodic=bool(payload.get("periodic", False)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CellSetBackendError(f"invalid cellSet settings: {error}") from error

    @classmethod
    def from_state(cls, state: CellSetState) -> CellSetConfig | None:
        """Recover the config recorded in a snapshot's provenance."""
        provenance = state.provenance
        height = provenance.get("height")
        depth = provenance.get("substrateDepth")
        if height is None or depth is None:
            return None
        try:
            return cls(
                height=height,
                substrate_depth=depth,
                cover_material=provenance.get("coverMaterial") or "GAS",
                periodic=bool(provenance.get("periodic", False)),
            )
        except ValueError as error:
            raise CellSetBackendError(f"invalid cell-set provenance config: {error}") from error

    def provenance(self) -> dict[str, Any]:
        """Return the JSON-safe provenance entries for this config."""
        return {
            "height": float(self.height),
            "substrateDepth": float(self.substrate_depth),
            "coverMaterial": self.cover_material,
            "periodic": bool(self.periodic),
        }

    def compatible_with(self, other: CellSetConfig) -> bool:
        """Return whether two configs describe the same native lattice policy."""
        return (
            abs(float(self.height) - float(other.height)) <= 1e-12
            and abs(float(self.substrate_depth) - float(other.substrate_depth)) <= 1e-12
            and self.cover_material == other.cover_material
            and bool(self.periodic) == bool(other.periodic)
        )


@runtime_checkable
class CellSetBackend(Protocol):
    """Engine-side native cell-set lifecycle.

    ``ensure`` derives a native cell set from the current geometry;
    ``snapshot`` converts the authoritative native handle to a persistable
    :class:`CellSetState`; ``rehydrate`` recreates the native handle from a
    snapshot plus the current geometry.  Conversion is deliberately coarse
    grained: it happens at sequence boundaries, not on every field step.
    """

    id: str
    version: str
    capabilities: CellSetCapabilities

    def geometry_digest(self, geometry: LevelSetState) -> str: ...

    def ensure(
        self,
        geometry: LevelSetState,
        context: ProcessContext,
        config: CellSetConfig,
        *,
        source_step_id: str | None = None,
    ) -> CellSetState: ...

    def snapshot(
        self,
        native_cell_set: Any,
        context: ProcessContext,
        *,
        base: CellSetState | None = None,
        geometry: LevelSetState | None = None,
        config: CellSetConfig | None = None,
        source_step_id: str | None = None,
    ) -> CellSetState: ...

    def rehydrate(
        self,
        geometry: LevelSetState,
        state: CellSetState,
        context: ProcessContext,
        config: CellSetConfig,
    ) -> CellSetState: ...

    def validate(
        self,
        state: CellSetState,
        context: ProcessContext,
        config: CellSetConfig | None = None,
    ) -> None: ...
