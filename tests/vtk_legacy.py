"""Small independent reader for the VTK legacy ASCII files used by v1.

This deliberately does not import ViennaPS and reads the exported file back
from disk, rather than validating an in-memory Mesh object.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


class VtkFormatError(ValueError):
    pass


def _expect(tokens: list[str], index: int, token: str) -> int:
    if index >= len(tokens) or tokens[index].upper() != token.upper():
        raise VtkFormatError(f"expected {token} at token {index}")
    return index + 1


def _number(tokens: list[str], index: int, context: str) -> tuple[float, int]:
    if index >= len(tokens):
        raise VtkFormatError(f"missing number in {context}")
    try:
        value = float(tokens[index])
    except ValueError as exc:
        raise VtkFormatError(f"invalid number in {context}: {tokens[index]!r}") from exc
    if not math.isfinite(value):
        raise VtkFormatError(f"non-finite number in {context}")
    return value, index + 1


def read_legacy_vtk(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    raw = source.read_text(encoding="ascii")
    lines = raw.splitlines()
    if len(lines) < 4 or not lines[0].startswith("# vtk DataFile Version"):
        raise VtkFormatError("not a VTK legacy file")
    if lines[2].strip().upper() != "ASCII":
        raise VtkFormatError("only VTK legacy ASCII is accepted")
    if lines[3].strip().upper() != "DATASET UNSTRUCTURED_GRID":
        raise VtkFormatError("expected DATASET UNSTRUCTURED_GRID")
    tokens = "\n".join(lines[4:]).split()

    try:
        i = tokens.index("POINTS")
    except ValueError as exc:
        raise VtkFormatError("POINTS section missing") from exc
    i += 1
    if i + 1 >= len(tokens):
        raise VtkFormatError("incomplete POINTS header")
    point_count = int(tokens[i])
    i += 2  # count and scalar type
    points = []
    for _ in range(point_count):
        point = []
        for _axis in range(3):
            value, i = _number(tokens, i, "POINTS")
            point.append(value)
        points.append(point)

    i = _expect(tokens, i, "CELLS")
    cell_count = int(tokens[i])
    total_size = int(tokens[i + 1])
    i += 2
    cells = []
    consumed = 0
    for _ in range(cell_count):
        n = int(tokens[i])
        i += 1
        consumed += n + 1
        if n <= 0 or i + n > len(tokens):
            raise VtkFormatError("invalid cell connectivity length")
        cell = [int(value) for value in tokens[i : i + n]]
        i += n
        if any(index < 0 or index >= point_count for index in cell):
            raise VtkFormatError("cell connectivity index is outside POINTS")
        cells.append(cell)
    if consumed != total_size:
        raise VtkFormatError(f"CELLS total size mismatch: expected {total_size}, read {consumed}")

    i = _expect(tokens, i, "CELL_TYPES")
    type_count = int(tokens[i])
    i += 1
    if type_count != cell_count:
        raise VtkFormatError("CELL_TYPES count does not match CELLS")
    cell_types = [int(value) for value in tokens[i : i + type_count]]
    i += type_count

    cell_data: dict[str, list[float]] = {}
    vectors: dict[str, list[list[float]]] = {}
    if i < len(tokens) and tokens[i].upper() == "CELL_DATA":
        i += 1
        data_count = int(tokens[i])
        i += 1
        if data_count != cell_count:
            raise VtkFormatError("CELL_DATA count does not match CELLS")
        while i < len(tokens):
            section = tokens[i].upper()
            if section == "SCALARS":
                if i + 3 >= len(tokens):
                    raise VtkFormatError("incomplete SCALARS header")
                label = tokens[i + 1]
                i += 3
                components = 1
                if tokens[i].upper() != "LOOKUP_TABLE":
                    components = int(tokens[i])
                    i += 1
                i = _expect(tokens, i, "LOOKUP_TABLE")
                i += 1  # lookup table name
                expected = data_count * components
                values = []
                for _ in range(expected):
                    value, i = _number(tokens, i, f"SCALARS {label}")
                    values.append(value)
                if components == 1:
                    cell_data[label] = values
                else:
                    cell_data[label] = values
            elif section in ("VECTORS", "NORMALS"):
                if i + 2 >= len(tokens):
                    raise VtkFormatError(f"incomplete {section} header")
                label = tokens[i + 1]
                i += 3
                vectors[label] = []
                for _ in range(data_count):
                    vector = []
                    for _axis in range(3):
                        value, i = _number(tokens, i, f"{section} {label}")
                        vector.append(value)
                    vectors[label].append(vector)
            else:
                raise VtkFormatError(f"unsupported CELL_DATA section {tokens[i]!r}")

    if any(len(values) != cell_count for values in cell_data.values()):
        raise VtkFormatError("scalar cell-data array length does not match CELLS")
    if any(len(values) != cell_count for values in vectors.values()):
        raise VtkFormatError("vector cell-data array length does not match CELLS")
    return {
        "path": str(source),
        "pointCount": point_count,
        "cellCount": cell_count,
        "cellTypes": cell_types,
        "points": points,
        "cells": cells,
        "cellData": cell_data,
        "cellVectors": vectors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--require-material-ids", action="store_true")
    args = parser.parse_args()
    result = read_legacy_vtk(args.path)
    if args.require_material_ids and "MaterialIds" not in result["cellData"]:
        raise VtkFormatError("MaterialIds cell data is missing")
    print(json.dumps({
        "path": result["path"],
        "pointCount": result["pointCount"],
        "cellCount": result["cellCount"],
        "cellTypes": sorted(set(result["cellTypes"])),
        "cellData": {name: {"count": len(values), "values": sorted(set(values))} for name, values in result["cellData"].items()},
        "vectorData": {name: {"count": len(values)} for name, values in result["cellVectors"].items()},
        "allFinite": all(math.isfinite(value) for point in result["points"] for value in point),
        "allConnectivityValid": all(0 <= index < result["pointCount"] for cell in result["cells"] for index in cell),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
