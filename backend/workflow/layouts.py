"""Bounded GDS polygon extraction and clip asset generation for P2.

The workflow layer only needs flat polygons for a selected structure/layer set
inside a rectangular clip.  This module flattens SREF/AREF references with the
standard GDS placement transform, clips polygons to the clip rectangle, and
writes a small derived GDS containing only those polygons.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Iterable

from ..gds import GdsFormatError, Library, _encode_real8, _encode_record, parse_gds

MAX_POLYGONS = 20_000
MAX_POINTS = 500_000
MAX_DEPTH = 24
MAX_POINTS_PER_POLYGON = 8_000
COORDINATE_LIMIT_UM = 1_000_000.0

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


@dataclass(frozen=True)
class RawElement:
    kind: int
    layer: int
    datatype: int
    points: tuple[tuple[float, float], ...]
    name: str | None
    columns: int
    rows: int
    mirror: bool
    magnification: float
    angle: float


def _decode_real8(payload: bytes) -> float:
    if len(payload) != 8:
        raise GdsFormatError("GDS real8 value must be eight bytes")
    sign = -1.0 if payload[0] & 0x80 else 1.0
    exponent = (payload[0] & 0x7F) - 64
    mantissa = int.from_bytes(payload[1:], "big")
    return sign * mantissa * (16.0 ** (exponent - 14))


def _elements(library: Library, structure_name: str) -> list[RawElement]:
    structure = library.find_structure(structure_name)
    elements: list[RawElement] = []
    current: dict[str, Any] | None = None
    for record in library.records:
        if not (structure.start <= record.start < structure.end):
            continue
        kind = record.kind
        payload = library.data[record.payload_start:record.payload_end]
        if kind in (0x08, 0x09, 0x0A, 0x0B, 0x0C):
            current = {
                "kind": kind, "layer": 0, "datatype": 0, "points": (),
                "name": None, "columns": 1, "rows": 1,
                "mirror": False, "magnification": 1.0, "angle": 0.0,
            }
        elif current is not None:
            if kind == 0x0D and len(payload) >= 2:
                current["layer"] = struct.unpack_from(">h", payload)[0]
            elif kind == 0x0E and len(payload) >= 2:
                current["datatype"] = struct.unpack_from(">h", payload)[0]
            elif kind == 0x10 and len(payload) >= 8 and len(payload) % 8 == 0:
                values = struct.unpack(">" + "i" * (len(payload) // 4), payload)
                current["points"] = tuple((values[index], values[index + 1]) for index in range(0, len(values), 2))
            elif kind == 0x12:
                current["name"] = payload.rstrip(b"\0").decode("ascii", errors="strict")
            elif kind == 0x13 and len(payload) >= 4:
                current["columns"], current["rows"] = struct.unpack_from(">hh", payload)
            elif kind == 0x1A and len(payload) >= 2:
                flags = struct.unpack_from(">H", payload)[0]
                current["mirror"] = bool(flags & 0x8000)
            elif kind == 0x1B and len(payload) == 8:
                current["magnification"] = _decode_real8(payload)
            elif kind == 0x1C and len(payload) == 8:
                current["angle"] = _decode_real8(payload)
            elif kind == 0x11:
                element_kind = current["kind"]
                points = current["points"]
                if element_kind == 0x08 and len(points) >= 4:
                    elements.append(RawElement(
                        kind=element_kind, layer=int(current["layer"]), datatype=int(current["datatype"]),
                        points=points, name=None, columns=1, rows=1, mirror=False,
                        magnification=1.0, angle=0.0,
                    ))
                elif element_kind in (0x0A, 0x0B) and current["name"] and points:
                    elements.append(RawElement(
                        kind=element_kind, layer=0, datatype=0, points=points,
                        name=str(current["name"]), columns=max(1, int(current["columns"])),
                        rows=max(1, int(current["rows"])), mirror=bool(current["mirror"]),
                        magnification=float(current["magnification"]), angle=float(current["angle"]),
                    ))
                current = None
    return elements


def _compose(parent: Matrix, child: Matrix) -> Matrix:
    pa, pb, pc, pd, pe, pf = parent
    ca, cb, cc, cd, ce, cf = child
    return (
        pa * ca + pb * cd, pa * cb + pb * ce, pa * cc + pb * cf + pc,
        pd * ca + pe * cd, pd * cb + pe * ce, pd * cc + pe * cf + pf,
    )


def _apply(matrix: Matrix, point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * point[0] + b * point[1] + c, d * point[0] + e * point[1] + f)


def _placement(origin: tuple[float, float], *, mirror: bool, magnification: float, angle: float) -> Matrix:
    if not math.isfinite(magnification) or magnification <= 0 or magnification > 1_000_000:
        raise GdsFormatError("GDS magnification must be finite and positive")
    if not math.isfinite(angle):
        raise GdsFormatError("GDS angle must be finite")
    theta = math.radians(angle)
    cosine, sine = math.cos(theta), math.sin(theta)
    if mirror:
        a, b = magnification * cosine, magnification * sine
        d, e = magnification * sine, -magnification * cosine
    else:
        a, b = magnification * cosine, -magnification * sine
        d, e = magnification * sine, magnification * cosine
    return (a, b, origin[0], d, e, origin[1])


def _flatten(
    library: Library,
    structure_name: str,
    matrix: Matrix = IDENTITY,
    depth: int = 0,
    polygons: list[tuple[int, int, list[tuple[float, float]]]] | None = None,
) -> list[tuple[int, int, list[tuple[float, float]]]]:
    if depth > MAX_DEPTH:
        raise GdsFormatError("GDS hierarchy exceeds the flattening depth limit")
    result = polygons if polygons is not None else []
    for element in _elements(library, structure_name):
        if element.kind == 0x08:
            transformed = [_apply(matrix, point) for point in element.points]
            if len(transformed) > 1 and transformed[0] == transformed[-1]:
                transformed.pop()
            if len(transformed) >= 3:
                result.append((element.layer, element.datatype, transformed))
            if len(result) > MAX_POLYGONS or sum(len(points) for _, _, points in result) > MAX_POINTS:
                raise GdsFormatError("GDS polygon extraction exceeds the bounded clip limits")
            continue
        if not element.name:
            raise GdsFormatError("GDS reference is missing a structure name")
        if element.kind == 0x0A:
            child = _placement(element.points[0], mirror=element.mirror,
                               magnification=element.magnification, angle=element.angle)
            _flatten(library, element.name, _compose(matrix, child), depth + 1, result)
        else:
            if len(element.points) < 3:
                raise GdsFormatError("GDS AREF requires three reference points")
            origin, column_end, row_end = element.points[0], element.points[1], element.points[2]
            columns, rows = element.columns, element.rows
            column_step = ((column_end[0] - origin[0]) / columns, (column_end[1] - origin[1]) / columns)
            row_step = ((row_end[0] - origin[0]) / rows, (row_end[1] - origin[1]) / rows)
            for column in range(columns):
                for row in range(rows):
                    offset = (
                        origin[0] + column * column_step[0] + row * row_step[0],
                        origin[1] + column * column_step[1] + row * row_step[1],
                    )
                    child = _placement(offset, mirror=element.mirror,
                                       magnification=element.magnification, angle=element.angle)
                    _flatten(library, element.name, _compose(matrix, child), depth + 1, result)
    return result


def _clip_polygon(points: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    def clip(polygon: list[tuple[float, float]], inside, intersect) -> list[tuple[float, float]]:
        if not polygon:
            return []
        output: list[tuple[float, float]] = []
        previous = polygon[-1]
        for current in polygon:
            if inside(current):
                if not inside(previous):
                    output.append(intersect(previous, current))
                output.append(current)
            elif inside(previous):
                output.append(intersect(previous, current))
            previous = current
        return output

    def left_inside(point): return point[0] >= x0
    def right_inside(point): return point[0] <= x1
    def bottom_inside(point): return point[1] >= y0
    def top_inside(point): return point[1] <= y1

    def vertical(a, b, x):
        if abs(b[0] - a[0]) < 1e-15:
            return (x, a[1])
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + t * (b[1] - a[1]))

    def horizontal(a, b, y):
        if abs(b[1] - a[1]) < 1e-15:
            return (a[0], y)
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), y)

    result = list(points)
    result = clip(result, left_inside, lambda a, b: vertical(a, b, x0))
    result = clip(result, right_inside, lambda a, b: vertical(a, b, x1))
    result = clip(result, bottom_inside, lambda a, b: horizontal(a, b, y0))
    result = clip(result, top_inside, lambda a, b: horizontal(a, b, y1))
    cleaned: list[tuple[float, float]] = []
    for point in result:
        if not cleaned or abs(point[0] - cleaned[-1][0]) > 1e-12 or abs(point[1] - cleaned[-1][1]) > 1e-12:
            cleaned.append(point)
    if len(cleaned) > 1 and cleaned[0] == cleaned[-1]:
        cleaned.pop()
    return cleaned


def _area(points: Iterable[tuple[float, float]]) -> float:
    values = list(points)
    return 0.5 * sum(
        values[index][0] * values[(index + 1) % len(values)][1]
        - values[(index + 1) % len(values)][0] * values[index][1]
        for index in range(len(values))
    )


def extract_clip_polygons(
    data: bytes,
    *,
    structure: str,
    layers: list[dict[str, Any]],
    bbox: dict[str, float],
) -> dict[str, Any]:
    library = parse_gds(data)
    x0, y0, x1, y1 = (float(bbox[key]) for key in ("x0", "y0", "x1", "y1"))
    if not all(math.isfinite(value) and abs(value) <= COORDINATE_LIMIT_UM for value in (x0, y0, x1, y1)):
        raise GdsFormatError("clip bounds must contain finite coordinates within ±1,000,000 µm")
    if x1 <= x0 or y1 <= y0:
        raise GdsFormatError("clip bounds must satisfy x0 < x1 and y0 < y1")
    selected = {(int(row["layer"]), row.get("datatype")) for row in layers}
    if not selected:
        raise GdsFormatError("clip requires at least one selected GDS layer")
    available = set(library.effective_layer_datatypes(structure))
    for layer, datatype in selected:
        if datatype is None:
            if not any(pair[0] == layer for pair in available):
                raise GdsFormatError(f"GDS layer {layer} is not present in structure {structure!r}")
        elif (layer, int(datatype)) not in available:
            raise GdsFormatError(f"GDS layer/datatype {layer}/{datatype} is not present in structure {structure!r}")
    flat = _flatten(library, structure)
    to_um = library.meters_per_database_unit * 1_000_000.0
    polygons: list[dict[str, Any]] = []
    point_count = 0
    content_x0 = content_y0 = math.inf
    content_x1 = content_y1 = -math.inf
    for layer, datatype, points in flat:
        if not any(layer == selected_layer and (datatype_value is None or datatype_value == datatype)
                   for selected_layer, datatype_value in selected):
            continue
        absolute = [(x * to_um, y * to_um) for x, y in points]
        clipped = _clip_polygon(absolute, x0, y0, x1, y1)
        if len(clipped) < 3 or abs(_area(clipped)) <= 1e-18:
            continue
        local = [(x - x0, y - y0) for x, y in clipped]
        point_count += len(local)
        if len(polygons) >= MAX_POLYGONS or point_count > MAX_POINTS:
            raise GdsFormatError("clip polygon extraction exceeds the bounded limits")
        for x, y in local:
            content_x0 = min(content_x0, x)
            content_y0 = min(content_y0, y)
            content_x1 = max(content_x1, x)
            content_y1 = max(content_y1, y)
        polygons.append({"layer": layer, "datatype": datatype, "points": [[round(x, 9), round(y, 9)] for x, y in local]})
    if not polygons:
        raise GdsFormatError("clip selection contains no boundary polygons")
    return {
        "structure": structure,
        "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
        "width": round(x1 - x0, 9),
        "height": round(y1 - y0, 9),
        "polygons": polygons,
        "polygonCount": len(polygons),
        "pointCount": point_count,
        "contentBounds": {
            "x0": round(content_x0, 9), "y0": round(content_y0, 9),
            "x1": round(content_x1, 9), "y1": round(content_y1, 9),
        },
    }


def _string_record(kind: int, data_type: int, value: str) -> bytes:
    payload = value.encode("ascii")
    if len(payload) % 2:
        payload += b"\0"
    return _encode_record(kind, data_type, payload)


def build_clip_gds(polygons: list[dict[str, Any]], width: float, height: float) -> bytes:
    """Write a deterministic single-structure GDS centred on the clip bounds."""

    timestamps = struct.pack(">" + "h" * 12, 2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)
    parts = [
        _encode_record(0x00, 0x02, struct.pack(">h", 600)),
        _encode_record(0x01, 0x02, timestamps),
        _string_record(0x02, 0x06, "CLIP"),
        _encode_record(0x03, 0x05, _encode_real8(0.001) + _encode_real8(1e-9)),
        _encode_record(0x05, 0x02, timestamps),
        _string_record(0x06, 0x06, "CLIP"),
    ]
    for polygon in polygons:
        points = polygon["points"]
        if len(points) > MAX_POINTS_PER_POLYGON:
            raise GdsFormatError("clip polygon exceeds the 8,000 point GDS record limit")
        coordinates: list[int] = []
        for x, y in points:
            coordinates.extend((int(round((float(x) - width / 2.0) * 1000.0)),
                                int(round((float(y) - height / 2.0) * 1000.0))))
        coordinates.extend((coordinates[0], coordinates[1]))
        if len(coordinates) % 2:
            coordinates.append(0)
        parts.append(_encode_record(0x08, 0x00, b""))
        parts.append(_encode_record(0x0D, 0x02, struct.pack(">h", int(polygon["layer"]))))
        parts.append(_encode_record(0x0E, 0x02, struct.pack(">h", int(polygon.get("datatype", 0)))))
        parts.append(_encode_record(0x10, 0x03, struct.pack(">" + "i" * len(coordinates), *coordinates)))
        parts.append(_encode_record(0x11, 0x00, b""))
    parts.append(_encode_record(0x07, 0x00, b""))
    parts.append(_encode_record(0x04, 0x00, b""))
    data = b"".join(parts)
    if len(data) > 64 * 1024 * 1024:
        raise GdsFormatError("generated clip GDS exceeds the 64 MiB asset limit")
    # Round-trip the generated bytes so an invalid clip asset can never be stored.
    parse_gds(data)
    return data


def save_asset(data: bytes) -> str:
    from .. import storage

    digest = __import__("hashlib").sha256(data).hexdigest()
    path = storage.gds_asset_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise GdsFormatError("content-addressed GDS asset collision")
    else:
        path.write_bytes(data)
    return digest


def canonical_clip_hash(document: dict[str, Any]) -> str:
    import hashlib
    import json

    payload = {
        "layoutId": document["layoutId"],
        "structure": document["structure"],
        "layers": document["layers"],
        "bbox": document["bbox"],
        "annotations": document["annotations"],
        "polygons": document["polygons"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def structure_bounds(data: bytes, structure: str) -> dict[str, float] | None:
    library = parse_gds(data)
    flat = _flatten(library, structure)
    to_um = library.meters_per_database_unit * 1_000_000.0
    xs: list[float] = []
    ys: list[float] = []
    for _layer, _datatype, points in flat:
        for x, y in points:
            xs.append(x * to_um)
            ys.append(y * to_um)
    if not xs:
        return None
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)}
