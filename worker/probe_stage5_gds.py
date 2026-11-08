"""Probe native GDS reading against a tiny two-structure, two-layer fixture.

Run this native probe only through scripts/bounded_child.py.
"""
from __future__ import annotations

import json
import struct
import sys
from importlib.metadata import version
from pathlib import Path


def record(kind: int, data_type: int, payload: bytes = b"") -> bytes:
    if data_type == 0x06 and len(payload) % 2:
        payload += b"\0"
    return struct.pack(">HBB", len(payload) + 4, kind, data_type) + payload


def int2(*values: int) -> bytes:
    return struct.pack(">" + "h" * len(values), *values)


def int4(*values: int) -> bytes:
    return struct.pack(">" + "i" * len(values), *values)


def real8(value: float) -> bytes:
    if value == 0:
        return b"\0" * 8
    sign = 0x80 if value < 0 else 0
    normalized = abs(value)
    exponent = 0
    while normalized < 1 / 16:
        normalized *= 16
        exponent -= 1
    while normalized >= 1:
        normalized /= 16
        exponent += 1
    mantissa = int(normalized * (1 << 56))
    first = sign | ((exponent + 64) & 0x7F)
    return bytes([first]) + mantissa.to_bytes(7, "big")


def gds_fixture(path: Path) -> None:
    records = [record(0x00, 0x02, int2(600)),
               record(0x01, 0x02, int2(2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)),
               record(0x02, 0x06, b"STAGE5") , record(0x03, 0x05, real8(0.001) + real8(1e-9))]
    structures = (("CELL_A", 1, (0, 0, 2000, 1000)), ("CELL_B", 2, (3000, 0, 5000, 2000)))
    for name, layer, (x0, y0, x1, y1) in structures:
        records.extend([record(0x05, 0x02, int2(2026, 1, 1, 0, 0, 0, 2026, 1, 1, 0, 0, 0)),
                        record(0x06, 0x06, name.encode("ascii")), record(0x08, 0x00),
                        record(0x0D, 0x02, int2(layer)), record(0x0E, 0x02, int2(0)),
                        record(0x10, 0x03, int4(x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)), record(0x11, 0x00),
                        record(0x07, 0x00)])
    records.append(record(0x04, 0x00))
    path.write_bytes(b"".join(records))


def main() -> None:
    import viennaps as ps

    output_dir = Path(sys.argv[1]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture = output_dir / "two-structure-two-layer.gds"
    gds_fixture(fixture)
    geometry = ps.d2.GDSGeometry(0.25)
    geometry.setBoundaryConditions([ps.BoundaryType.REFLECTIVE_BOUNDARY, ps.BoundaryType.INFINITE_BOUNDARY])
    print("load 2d GDS", flush=True)
    reader = ps.d2.GDSReader(geometry, str(fixture))
    reader.apply()
    report = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "fixture": {"file": fixture.name, "sizeBytes": fixture.stat().st_size},
        "nativeReadback": {
            "numberOfStructures": geometry.getNumberOfStructures(),
            "layers": sorted(int(layer) for layer in geometry.getAllLayers()),
            "bounds": [float(value) for value in geometry.getBounds()],
            "gridDelta": 0.25,
        },
        "layerResults": {},
    }
    for layer in sorted(int(item) for item in geometry.getAllLayers()):
        print(f"convert 2d layer {layer}", flush=True)
        level_set = geometry.layerToLevelSet(layer, False)
        fields = {}
        for name in dir(level_set):
            if name.startswith("_"):
                continue
            try:
                value = getattr(level_set, name)
                if callable(value):
                    continue
                fields[name] = str(value)
            except BaseException as error:
                fields[name] = f"{type(error).__name__}: {error}"
        methods = [name for name in dir(level_set) if not name.startswith("_") and callable(getattr(level_set, name, None))]
        result = {"type": type(level_set).__name__, "availableMethods": methods, "readbackFields": fields}
        for name in ("getGridDelta", "getNumberOfPoints", "getNumberOfCells", "getBounds", "getBoundingBox", "getNumberOfLevelSets"):
            method = getattr(level_set, name, None)
            if callable(method):
                try:
                    value = method()
                    result[name] = value if isinstance(value, (int, float, str, list, tuple)) else str(value)
                except BaseException as error:
                    result[f"{name}Error"] = f"{type(error).__name__}: {error}"
        report["layerResults"][str(layer)] = result
    print("load 3d GDS", flush=True)
    domain3 = ps.d3.Domain(0.25, 10.0, 6.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    print("construct 3d GDS geometry", flush=True)
    geometry3 = ps.d3.GDSGeometry(0.25)
    geometry3.setBoundaryConditions([ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                     ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                     ps.BoundaryType.INFINITE_BOUNDARY])
    print("construct 3d GDS reader", flush=True)
    ps.d3.GDSReader(geometry3, str(fixture)).apply()
    print("3d GDS read", flush=True)
    report["native3DReadback"] = {
        "numberOfStructures": geometry3.getNumberOfStructures(),
        "layers": sorted(int(layer) for layer in geometry3.getAllLayers()),
        "bounds": [float(value) for value in geometry3.getBounds()],
    }
    report["layer3DResults"] = {}
    for layer in sorted(int(item) for item in geometry3.getAllLayers()):
        print(f"convert 3d layer {layer}", flush=True)
        level_set = geometry3.layerToLevelSet(layer, 0.2, 1.0, False, False)
        domain3.insertNextLevelSetAsMaterial(level_set, ps.Material.Si, False)
        mesh = domain3.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        points = [[float(value) for value in point] for point in mesh.getNodes()]
        triangles = [[int(value) for value in triangle] for triangle in mesh.getTriangles()]
        report["layer3DResults"][str(layer)] = {
            "pointCount": len(points), "triangleCount": len(triangles),
            "bounds": [[min(row[axis] for row in points), max(row[axis] for row in points)] for axis in range(3)] if points else None,
            "materialMap": [int(domain3.getMaterialMap().getMaterialIdAtIdx(index)) for index in range(domain3.getMaterialMap().size())],
        }
    output = output_dir / "gds-native-probe.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "readback": report["nativeReadback"],
                      "layerCount": len(report["layerResults"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
