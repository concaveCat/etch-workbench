"""Probe installed ViennaPS Writer/Reader round trips; run via bounded_child.py."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import viennaps as ps


def signature(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells = [[int(value) for value in cell] for cell in (mesh.getLines() if dimension == 2 else mesh.getTriangles())]
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    material_map = [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]
    encoded = json.dumps({"points": points, "cells": cells, "materialIds": material_ids, "materialMap": material_map},
                         separators=(",", ":"), allow_nan=False).encode()
    return {"points": len(points), "cells": len(cells), "materialIds": sorted(set(material_ids)),
            "materialMap": material_map, "sha256": hashlib.sha256(encoded).hexdigest(),
            "boundaryConditions": [str(value) for value in domain.getBoundaryConditions()],
            "gridDelta": float(domain.getGridDelta()), "boundingBox": domain.getBoundingBox(),
            "levelSetCount": int(domain.getNumberOfLevelSets())}


def make_domain(dimension):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = ps.Domain(0.2, 4.0, boundary) if dimension == 2 else ps.Domain(0.2, 4.0, 4.0, boundary)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=0.6, material=ps.Material.SiO2, addToExisting=True).apply()
    ps.MakePlane(domain, height=1.0, material=ps.Material.Si3N4, addToExisting=True).apply()
    return domain


def main():
    evidence = Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    output = {"versions": {"viennaps": ps.version, "viennals": ps.ls.version}, "dimensions": {}}
    for dimension in (2, 3):
        domain = make_domain(dimension)
        before = signature(domain, dimension)
        filename = evidence / f"writer-reader-{dimension}d.vpsd"
        writer = ps.Writer(domain, str(filename))
        writer.apply()
        if not filename.is_file() or filename.stat().st_size == 0:
            raise RuntimeError(f"ViennaPS Writer did not create a nonempty .vpsd for {dimension}D")
        if dimension == 2:
            restored = ps.Domain(0.2, 4.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
        else:
            restored = ps.Domain(0.2, 4.0, 4.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
        reader = ps.Reader(restored, str(filename))
        reader.apply()
        after = signature(restored, dimension)
        if before != after:
            raise RuntimeError(f"ViennaPS Writer/Reader did not preserve the {dimension}D domain: {before} != {after}")
        output["dimensions"][str(dimension)] = {"path": filename.name, "bytes": filename.stat().st_size,
                                                "before": before, "after": after, "exactSignatureMatch": True}
    (evidence / "writer-reader-probe.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"versions": output["versions"], "dimensions": sorted(output["dimensions"])}))


if __name__ == "__main__":
    main()
