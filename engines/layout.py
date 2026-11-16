"""Layout access for the comparison engines (GDS today, OASIS via gdstk).

Why this module exists: SPX resolves layout content by *its own* layer names
(``PLG``/``CS``/``M1``...), and headlessly it will not tell us how those map to the
layer numbers stored in the file. The file itself can: enumerate the layers, their
polygon counts and their extents here, then use those numbers on both sides.

Two readers, one vocabulary:

* GDS  -> :mod:`backend.gds` (pure Python, already used by the workbench);
* OASIS -> ``gdstk`` when importable (a one-wheel dependency, not Synopsys code),
  converted to GDS in memory with **explicit** user unit / database unit so the
  coordinates are identical on both sides.

Returns micrometres. Nothing here starts SPX or imports viennaps.
"""

from __future__ import annotations

import importlib
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USER_UNIT_M = 1e-6      # 1 um per user unit
DATABASE_UNIT_M = 1e-9  # 1 nm per database unit


class LayoutError(RuntimeError):
    """The layout could not be read or converted."""


@dataclass(frozen=True)
class LayerSummary:
    layer: int
    datatype: int
    polygons: int
    bounds_um: tuple[float, float, float, float] | None

    def as_json(self) -> dict[str, Any]:
        return {"layer": self.layer, "datatype": self.datatype,
                "polygons": self.polygons,
                "boundsUm": list(self.bounds_um) if self.bounds_um else None}


@dataclass(frozen=True)
class LayoutSummary:
    path: str
    cells: list[str]
    extent_um: tuple[float, float, float, float] | None
    layers: list[LayerSummary]
    source_format: str

    def as_json(self) -> dict[str, Any]:
        return {"path": self.path, "cells": self.cells,
                "extentUm": list(self.extent_um) if self.extent_um else None,
                "layers": [layer.as_json() for layer in self.layers],
                "format": self.source_format}

    def layer_numbers(self) -> list[tuple[int, int]]:
        return [(layer.layer, layer.datatype) for layer in self.layers]


def _gdstk():
    try:
        return importlib.import_module("gdstk")
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the env
        raise LayoutError(
            "OASIS support needs gdstk (pip install gdstk); GDS files need no extra "
            "dependency") from exc


def to_gds(path: str | Path, target: str | Path | None = None) -> Path:
    """Convert an OASIS layout to GDS with explicit 1 um / 1 nm units.

    Measured on the Memory1 example: both formats then give identical bounds
    ``(0, 0, 0.624, 0.74) um`` and the same six layer numbers, so the conversion
    does not change what either engine sees.
    """
    gdstk = _gdstk()
    source = Path(path)
    library = gdstk.read_oas(str(source))
    library.unit = USER_UNIT_M
    library.precision = DATABASE_UNIT_M
    destination = Path(target) if target else source.with_suffix(".gds")
    library.write_gds(str(destination))
    return destination


def summarise(path: str | Path) -> LayoutSummary:
    """Layer map of a layout file, in micrometres, without starting any engine."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix in (".oas", ".oasis"):
        return _summarise_with_gdstk(source)
    return _summarise_gds(source)


def _summarise_with_gdstk(source: Path) -> LayoutSummary:
    gdstk = _gdstk()
    library = gdstk.read_oas(str(source))
    polygon_counts: dict[tuple[int, int], int] = {}
    bounds: dict[tuple[int, int], list[float]] = {}
    overall: list[float] | None = None
    for cell in library.cells:
        for polygon in cell.polygons:
            key = (int(polygon.layer), int(polygon.datatype))
            polygon_counts[key] = polygon_counts.get(key, 0) + 1
            for x, y in polygon.points:
                box = bounds.setdefault(key, [float(x), float(y), float(x), float(y)])
                box[0] = min(box[0], float(x))
                box[1] = min(box[1], float(y))
                box[2] = max(box[2], float(x))
                box[3] = max(box[3], float(y))
                if overall is None:
                    overall = [float(x), float(y), float(x), float(y)]
                else:
                    overall[0] = min(overall[0], float(x))
                    overall[1] = min(overall[1], float(y))
                    overall[2] = max(overall[2], float(x))
                    overall[3] = max(overall[3], float(y))
    layers = [LayerSummary(layer, datatype, polygon_counts[(layer, datatype)],
                           tuple(bounds[(layer, datatype)]))
              for layer, datatype in sorted(polygon_counts)]
    return LayoutSummary(str(source), [cell.name for cell in library.cells],
                         tuple(overall) if overall else None, layers, "oasis")


def _gds_real(raw: bytes) -> float:
    """Decode GDSII's 8-byte real (sign, base-16 exponent excess 64, 56-bit mantissa)."""
    from math import ldexp

    if not any(raw):
        return 0.0
    sign = -1.0 if raw[0] & 0x80 else 1.0
    exponent = (raw[0] & 0x7F) - 64
    mantissa = int.from_bytes(raw[1:8], "big") / float(1 << 56)
    return sign * mantissa * (16.0 ** exponent)


def read_gds_polygons(source: Path) -> tuple[list[str], float, list[tuple[int, int, list[tuple[float, float]]]]]:
    """Return (cell names, um per database unit, polygons) from a GDSII file.

    Written by hand instead of borrowing :mod:`backend.gds` internals: the parser
    there keeps raw record offsets, and this module must stay importable from the
    SPX interpreter, which has neither the backend nor numpy.
    """
    data = source.read_bytes()
    if len(data) < 64:
        raise LayoutError(f"{source} is too small to be a GDS file")
    cursor = 0
    unit_scale = 1e-3  # assume 1 nm dbu until UNITS says otherwise
    cells: list[str] = []
    polygons: list[tuple[int, int, list[tuple[float, float]]]] = []
    layer = datatype = 0
    while cursor + 4 <= len(data):
        length = int.from_bytes(data[cursor:cursor + 2], "big")
        if length < 4 or cursor + length > len(data):
            raise LayoutError(f"invalid GDS record length at byte {cursor}")
        kind = data[cursor + 2]
        payload = data[cursor + 4:cursor + length]
        if kind == 0x03 and len(payload) >= 16:  # UNITS
            unit_scale = _gds_real(payload[8:16]) * 1e6  # meters/dbu -> um/dbu
        elif kind == 0x06:  # STRNAME
            cells.append(payload.rstrip(b"\x00").decode("latin-1"))
        elif kind == 0x0D:  # LAYER
            layer = int.from_bytes(payload[:2], "big", signed=True)
        elif kind == 0x0E:  # DATATYPE
            datatype = int.from_bytes(payload[:2], "big", signed=True)
        elif kind == 0x10:  # XY
            values = [int.from_bytes(payload[i:i + 4], "big", signed=True)
                      for i in range(0, len(payload) - 3, 4)]
            points = [(values[i] * unit_scale, values[i + 1] * unit_scale)
                      for i in range(0, len(values) - 1, 2)]
            polygons.append((layer, datatype, points))
        cursor += length
    return cells, unit_scale, polygons


def _summarise_gds(source: Path) -> LayoutSummary:
    cells, _, polygons = read_gds_polygons(source)
    counts: dict[tuple[int, int], int] = {}
    boxes: dict[tuple[int, int], list[float]] = {}
    overall: list[float] | None = None
    for layer, datatype, points in polygons:
        key = (layer, datatype)
        counts[key] = counts.get(key, 0) + 1
        for x, y in points:
            box = boxes.setdefault(key, [x, y, x, y])
            box[0] = min(box[0], x)
            box[1] = min(box[1], y)
            box[2] = max(box[2], x)
            box[3] = max(box[3], y)
            if overall is None:
                overall = [x, y, x, y]
            else:
                overall[0] = min(overall[0], x)
                overall[1] = min(overall[1], y)
                overall[2] = max(overall[2], x)
                overall[3] = max(overall[3], y)
    layers = [LayerSummary(layer, datatype, counts[(layer, datatype)],
                           tuple(boxes[(layer, datatype)]))
              for layer, datatype in sorted(counts)]
    return LayoutSummary(str(source), cells, tuple(overall) if overall else None,
                         layers, "gds")
