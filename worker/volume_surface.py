"""Extract compact, closed per-material triangle shells from ViennaPS tetrahedral VTU."""

from __future__ import annotations

import base64
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any


_SCALARS = {
    "Int8": ("b", 1), "UInt8": ("B", 1),
    "Int16": ("h", 2), "UInt16": ("H", 2),
    "Int32": ("i", 4), "UInt32": ("I", 4),
    "Int64": ("q", 8), "UInt64": ("Q", 8),
    "Float32": ("f", 4), "Float64": ("d", 8),
}


def _local_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if child.tag.rsplit("}", 1)[-1] == name]


def _header(data: bytes, offset: int, width: int, endian: str) -> int:
    code = "I" if width == 4 else "Q"
    if offset < 0 or offset + width > len(data):
        raise ValueError("VTU block header is truncated")
    return int(struct.unpack_from(endian + code, data, offset)[0])


def _decode_encoded_block(encoded: str, offset: int, width: int, endian: str, compressed: bool) -> bytes:
    if offset < 0 or offset >= len(encoded):
        raise ValueError("VTU appended array offset is out of range")
    first_chars = ((width + 2) // 3) * 4
    first_header = base64.b64decode(encoded[offset:offset + first_chars])
    if len(first_header) < width:
        raise ValueError("VTU array header is truncated")
    if not compressed:
        byte_length = _header(first_header, 0, width, endian)
        payload_chars = ((byte_length + 2) // 3) * 4
        payload = base64.b64decode(encoded[offset + first_chars:offset + first_chars + payload_chars])
        if len(payload) < byte_length:
            raise ValueError("VTU appended array payload is truncated")
        return payload[:byte_length]

    block_count = _header(first_header, 0, width, endian)
    if block_count == 0:
        return b""
    if block_count > 1_000_000:
        raise ValueError("VTU compressed block count is invalid")
    header_bytes = (3 + block_count) * width
    header_chars = ((header_bytes + 2) // 3) * 4
    header = base64.b64decode(encoded[offset:offset + header_chars])
    if len(header) < header_bytes:
        raise ValueError("VTU compressed block table is truncated")
    block_size = _header(header, width, width, endian)
    last_block_size = _header(header, 2 * width, width, endian)
    compressed_sizes = [_header(header, (3 + index) * width, width, endian) for index in range(block_count)]
    compressed_length = sum(compressed_sizes)
    payload_chars = ((compressed_length + 2) // 3) * 4
    payload = base64.b64decode(encoded[offset + header_chars:offset + header_chars + payload_chars])
    if len(payload) < compressed_length:
        raise ValueError("VTU compressed payload is truncated")

    result = bytearray()
    cursor = 0
    for index, compressed_size in enumerate(compressed_sizes):
        end = cursor + compressed_size
        block = zlib.decompress(payload[cursor:end])
        expected = last_block_size if index == block_count - 1 else block_size
        if len(block) != expected:
            raise ValueError("VTU decompressed block size does not match its header")
        result.extend(block)
        cursor = end
    return bytes(result)


def _array_bytes(element: ET.Element, appended: str, width: int, endian: str, compressed: bool) -> bytes:
    scalar_type = element.get("type")
    if scalar_type not in _SCALARS:
        raise ValueError(f"unsupported VTU scalar type: {scalar_type}")
    fmt, scalar_width = _SCALARS[scalar_type]
    data_format = element.get("format", "ascii")
    if data_format == "appended":
        offset = int(element.get("offset", "-1"))
        data = _decode_encoded_block(appended, offset, width, endian, compressed)
    elif data_format == "binary":
        encoded = re.sub(r"\s+", "", element.text or "")
        data = _decode_encoded_block(encoded, 0, width, endian, compressed)
    elif data_format == "ascii":
        values = (element.text or "").split()
        if not values:
            return b""
        data = struct.pack(endian + str(len(values)) + fmt, *(float(value) if fmt in "fd" else int(value) for value in values))
    else:
        raise ValueError(f"unsupported VTU DataArray format: {data_format}")
    if len(data) % scalar_width:
        raise ValueError(f"VTU {scalar_type} array has a partial scalar value")
    return data


def _required_array(section: ET.Element, name: str) -> ET.Element:
    for element in _local_children(section, "DataArray"):
        if element.get("Name") == name:
            return element
    raise ValueError(f"ViennaPS volume mesh is missing its {name} array")


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _outward_face(points: list[tuple[float, float, float]], nodes: tuple[int, int, int, int], material_id: int) -> tuple[int, tuple[int, int, int], bool]:
    a, b, c, opposite = nodes
    pa, pb, pc, inside = points[a], points[b], points[c], points[opposite]
    normal = _cross((pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]), (pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]))
    center = ((pa[0] + pb[0] + pc[0]) / 3, (pa[1] + pb[1] + pc[1]) / 3, (pa[2] + pb[2] + pc[2]) / 3)
    inward_dot = normal[0] * (inside[0] - center[0]) + normal[1] * (inside[1] - center[1]) + normal[2] * (inside[2] - center[2])
    if not math.isfinite(inward_dot):
        raise ValueError("VTU contains a non-finite tetrahedron face orientation")
    # Match ViennaPS's native viewer: zero-area faces still participate in
    # index-based adjacency, but cannot provide a geometric outward direction.
    # Keeping the input order is harmless visually and preserves shell topology.
    triangle = (a, c, b) if inward_dot > 0 else (a, b, c)
    orientation_known = normal != (0.0, 0.0, 0.0) and inward_dot != 0.0
    return material_id, triangle, orientation_known


def _orient_closed_shell(triangles: list[tuple[int, int, int]], orientation_known: list[bool]) -> list[tuple[int, int, int]]:
    """Make a closed triangle shell consistently wound, keeping native outward votes."""
    edge_uses: dict[tuple[int, int], tuple[int, int, int | None, int | None]] = {}
    adjacent: list[list[tuple[int, bool]]] = [[] for _ in triangles]
    for face_index, (a, b, c) in enumerate(triangles):
        for start, end in ((a, b), (b, c), (c, a)):
            edge = (start, end) if start < end else (end, start)
            direction = 1 if start < end else -1
            prior = edge_uses.get(edge)
            if prior is None:
                edge_uses[edge] = (face_index, direction, None, None)
            elif prior[2] is None:
                other_index, other_direction = prior[0], prior[1]
                edge_uses[edge] = (other_index, other_direction, face_index, direction)
                same_direction = other_direction == direction
                adjacent[other_index].append((face_index, same_direction))
                adjacent[face_index].append((other_index, same_direction))
            else:
                raise ValueError("material shell has a non-manifold edge with more than two incident faces")

    if any(row[2] is None for row in edge_uses.values()):
        raise ValueError("material shell is open: an edge has only one incident face")

    flips: list[bool | None] = [None] * len(triangles)
    components: list[list[int]] = []
    for seed in range(len(triangles)):
        if flips[seed] is not None:
            continue
        flips[seed] = False
        component: list[int] = []
        stack = [seed]
        while stack:
            face_index = stack.pop()
            component.append(face_index)
            current_flip = bool(flips[face_index])
            for neighbor, same_direction in adjacent[face_index]:
                wanted_flip = current_flip ^ same_direction
                if flips[neighbor] is None:
                    flips[neighbor] = wanted_flip
                    stack.append(neighbor)
                elif flips[neighbor] != wanted_flip:
                    raise ValueError("material shell has inconsistent edge orientation constraints")
        components.append(component)

    # _outward_face already orients each non-degenerate face away from its
    # tetrahedron. Choose the consistent winding that preserves the most of
    # those native geometric decisions; degenerate faces supply no vote.
    for component in components:
        native_votes = sum((1 if not flips[index] else -1) for index in component if orientation_known[index])
        if native_votes < 0:
            for index in component:
                flips[index] = not flips[index]
    return [
        (triangle[0], triangle[2], triangle[1]) if flips[index] else triangle
        for index, triangle in enumerate(triangles)
    ]


def extract_closed_material_surface(path: Path) -> dict[str, Any]:
    """Return only boundary triangles and referenced points, preserving material IDs."""
    root = ET.fromstring(path.read_bytes())
    if root.tag.rsplit("}", 1)[-1] != "VTKFile" or root.get("type") != "UnstructuredGrid":
        raise ValueError("ViennaPS output is not an UnstructuredGrid VTU")
    header_type = root.get("header_type", "UInt32")
    if header_type not in ("UInt32", "UInt64"):
        raise ValueError(f"unsupported VTU header type: {header_type}")
    width = 8 if header_type == "UInt64" else 4
    endian = ">" if root.get("byte_order") == "BigEndian" else "<"
    compressor = root.get("compressor")
    if compressor not in (None, "vtkZLibDataCompressor"):
        raise ValueError(f"unsupported VTU compressor: {compressor}")
    appended_nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "AppendedData"]
    appended = ""
    if appended_nodes:
        if appended_nodes[0].get("encoding", "raw") != "base64":
            raise ValueError("raw binary VTU output cannot be decoded for shell extraction")
        appended = re.sub(r"\s+", "", appended_nodes[0].text or "").lstrip("_")
    pieces = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Piece"]
    if len(pieces) != 1:
        raise ValueError("ViennaPS volume mesh must contain exactly one UnstructuredGrid Piece")
    piece = pieces[0]
    point_count = int(piece.get("NumberOfPoints", "0"))
    cell_count = int(piece.get("NumberOfCells", "0"))
    if point_count < 1 or cell_count < 1:
        raise ValueError("ViennaPS volume mesh has invalid point or cell counts")

    sections = {child.tag.rsplit("}", 1)[-1]: child for child in list(piece)}
    point_arrays = _local_children(sections.get("Points", ET.Element("Points")), "DataArray")
    if not point_arrays or len(point_arrays) != 1 or int(point_arrays[0].get("NumberOfComponents", "1")) != 3:
        raise ValueError("ViennaPS volume mesh has invalid point coordinates")
    cells = sections.get("Cells")
    cell_data = sections.get("CellData")
    if cells is None or cell_data is None:
        raise ValueError("ViennaPS volume mesh is missing cell topology or material data")
    point_data = _array_bytes(point_arrays[0], appended, width, endian, bool(compressor))
    connectivity_element = _required_array(cells, "connectivity")
    offsets_element = _required_array(cells, "offsets")
    types_element = _required_array(cells, "types")
    material_element = _required_array(cell_data, "Material")
    connectivity = _array_bytes(connectivity_element, appended, width, endian, bool(compressor))
    offsets = _array_bytes(offsets_element, appended, width, endian, bool(compressor))
    types = _array_bytes(types_element, appended, width, endian, bool(compressor))
    materials = _array_bytes(material_element, appended, width, endian, bool(compressor))
    point_fmt, point_width = _SCALARS[point_arrays[0].get("type", "")]
    conn_fmt, conn_width = _SCALARS[connectivity_element.get("type", "")]
    offset_fmt, offset_width = _SCALARS[offsets_element.get("type", "")]
    type_fmt, type_width = _SCALARS[types_element.get("type", "")]
    material_fmt, material_width = _SCALARS[material_element.get("type", "")]
    if len(point_data) != point_count * 3 * point_width:
        raise ValueError("VTU point coordinates do not match NumberOfPoints")
    if len(offsets) != cell_count * offset_width or len(types) != cell_count * type_width or len(materials) != cell_count * material_width:
        raise ValueError("VTU cell topology or material data does not match NumberOfCells")
    if len(connectivity) % conn_width:
        raise ValueError("VTU connectivity array has a partial scalar value")

    points: list[tuple[float, float, float]] = []
    scalar_iter = struct.iter_unpack(endian + point_fmt, point_data)
    values = (row[0] for row in scalar_iter)
    for _ in range(point_count):
        point = (float(next(values)), float(next(values)), float(next(values)))
        if not all(math.isfinite(value) for value in point):
            raise ValueError("VTU point coordinates contain a non-finite value")
        points.append(point)

    local_faces = ((0, 1, 2, 3), (0, 3, 1, 2), (1, 3, 2, 0), (2, 3, 0, 1))
    pending: dict[tuple[int, int, int], tuple[int, tuple[int, int, int], bool]] = {}
    surfaces: dict[int, list[tuple[tuple[int, int, int], bool]]] = {}
    previous = 0
    connectivity_count = len(connectivity) // conn_width
    for cell in range(cell_count):
        end = int(struct.unpack_from(endian + offset_fmt, offsets, cell * offset_width)[0])
        cell_type = int(struct.unpack_from(endian + type_fmt, types, cell * type_width)[0])
        material_id = int(struct.unpack_from(endian + material_fmt, materials, cell * material_width)[0])
        if end - previous != 4 or cell_type != 10:
            raise ValueError("ViennaPS volume mesh contains a non-tetrahedral or malformed cell")
        nodes = tuple(int(value) for value in struct.unpack_from(endian + "4" + conn_fmt, connectivity, previous * conn_width))
        if len(set(nodes)) != 4 or any(index < 0 or index >= point_count for index in nodes):
            raise ValueError("VTU tetrahedron contains invalid point indices")
        previous = end
        for a, b, c, opposite in local_faces:
            face_material, triangle, orientation_known = _outward_face(points, (nodes[a], nodes[b], nodes[c], nodes[opposite]), material_id)
            key = tuple(sorted(triangle))
            sibling = pending.pop(key, None)
            if sibling is None:
                pending[key] = (face_material, triangle, orientation_known)
            elif sibling[0] != face_material:
                surfaces.setdefault(sibling[0], []).append((sibling[1], sibling[2]))
                surfaces.setdefault(face_material, []).append((triangle, orientation_known))
    if previous != connectivity_count:
        raise ValueError("VTU connectivity length does not match its cell offsets")
    for material_id, triangle, orientation_known in pending.values():
        surfaces.setdefault(material_id, []).append((triangle, orientation_known))
    if not surfaces:
        raise ValueError("ViennaPS volume mesh produced no material boundary faces")
    surfaces = {
        material_id: _orient_closed_shell(
            [row[0] for row in faces], [row[1] for row in faces]
        )
        for material_id, faces in surfaces.items()
    }

    used_points = sorted({point for triangles in surfaces.values() for triangle in triangles for point in triangle})
    remap = {source: target for target, source in enumerate(used_points)}
    compact_points = [points[index] for index in used_points]
    triangles_by_material = {
        str(material_id): [[remap[index] for index in triangle] for triangle in triangles]
        for material_id, triangles in sorted(surfaces.items())
    }
    return {"points": compact_points, "trianglesByMaterial": triangles_by_material}


def write_closed_surface_vtk(path: Path, surface: dict[str, Any]) -> tuple[int, int, list[int]]:
    points = surface["points"]
    triangles_by_material = surface["trianglesByMaterial"]
    rows = [(int(material_id), triangle) for material_id, triangles in triangles_by_material.items() for triangle in triangles]
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("# vtk DataFile Version 3.0\nViennaPS closed material boundary shells\nASCII\nDATASET POLYDATA\n")
        stream.write(f"POINTS {len(points)} double\n")
        for point in points:
            stream.write(f"{point[0]:.17g} {point[1]:.17g} {point[2]:.17g}\n")
        stream.write(f"POLYGONS {len(rows)} {len(rows) * 4}\n")
        for _, triangle in rows:
            stream.write(f"3 {triangle[0]} {triangle[1]} {triangle[2]}\n")
        stream.write(f"CELL_DATA {len(rows)}\nSCALARS MaterialIds int 1\nLOOKUP_TABLE default\n")
        for material_id, _ in rows:
            stream.write(f"{material_id}\n")
    return len(points), len(rows), sorted({material_id for material_id, _ in rows})
