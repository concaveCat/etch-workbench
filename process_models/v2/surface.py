"""Engine-neutral surface projection used by the viewer.

A level-set engine can expose its surface as points/cells/material IDs without
the platform knowing its native handle type.  ViennaPS and a custom engine may
implement this differently; ``worker`` only consumes ``SurfaceData``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .state import ProcessState

__all__ = ["SurfaceBackend", "SurfaceData"]


@dataclass(frozen=True)
class SurfaceData:
    """Renderable surface projection.

    ``cells`` contains triangles (3 indices) when ``cell_type`` is
    ``"triangles"`` and line segments (2 indices) when it is ``"lines"``.
    ``material_ids`` is one entry per cell, matching the viewer's cell data.
    """

    dimension: int
    points: tuple[tuple[float, float, float], ...]
    cells: tuple[tuple[int, ...], ...]
    cell_type: str
    material_ids: tuple[int, ...]
    grid_delta: float
    materials: Mapping[int, str] | None = None
    bounds: tuple[float, float, float, float, float, float] | None = None

    def __post_init__(self) -> None:
        if self.dimension not in (2, 3):
            raise ValueError("surface dimension must be 2 or 3")
        if self.cell_type not in ("triangles", "lines"):
            raise ValueError("surface cell_type must be 'triangles' or 'lines'")
        width = 3 if self.cell_type == "triangles" else 2
        if any(len(cell) != width for cell in self.cells):
            raise ValueError(f"every {self.cell_type} cell must have {width} indices")
        if len(self.material_ids) != len(self.cells):
            raise ValueError("surface material_ids must be one entry per cell")


class SurfaceBackend(Protocol):
    """One engine's renderable-surface extractor."""

    id: str

    def extract(self, state: ProcessState) -> SurfaceData: ...
