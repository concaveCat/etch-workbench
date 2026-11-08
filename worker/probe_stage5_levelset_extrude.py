"""Probe the installed ViennaLS Extrude operator on ViennaPS Fin level sets.

Run this native probe only through scripts/bounded_child.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps


def report_mesh(domain):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    cells = [[int(value) for value in row] for row in mesh.getTriangles()]
    data = mesh.getCellData()
    materials = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            materials = [int(value) for value in data.getScalarData(index)]
    return {"points": points, "triangles": cells, "materialIds": materials,
            "bounds": [[min(point[axis] for point in points), max(point[axis] for point in points)] for axis in range(3)]}


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    delta, x_extent, y_extent = 0.2, 4.0, 2.0
    ps.setDimension(2)
    input_domain = ps.d2.Domain(delta, x_extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.d2.MakeFin(input_domain, 0.8, 1.2, 5.0, 0.4, 0.0, False, ps.Material.Si, ps.Material.Mask).apply()
    level_sets = input_domain.getLevelSets()
    material_map = input_domain.getMaterialMap()

    output_domain = ps.d3.Domain(delta, x_extent, y_extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    records = []
    for index, input_level_set in enumerate(level_sets):
        material_id = int(material_map.getMaterialIdAtIdx(index))
        output_level_set = ps.ls.d3.Domain(delta)
        extrude = ps.ls.Extrude()
        extrude.setInputLevelSet(input_level_set)
        extrude.setOutputLevelSet(output_level_set)
        extrude.setExtent([-1.0, 1.0])
        extrude.setExtrusionAxis(1)
        extrude.apply()
        output_domain.insertNextLevelSetAsMaterial(output_level_set, ps.Material(material_id), False)
        records.append({"index": index, "materialId": material_id, "inputPoints": int(input_level_set.getNumberOfPoints()),
                        "outputPoints": int(output_level_set.getNumberOfPoints())})
    result = {"sourceLevelSets": records, "nativeMesh": report_mesh(output_domain),
              "materialMap": [int(output_domain.getMaterialMap().getMaterialIdAtIdx(index)) for index in range(output_domain.getMaterialMap().size())]}
    path = output / "levelset-extrude.json"
    path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(path), "sourceLevelSets": records,
                      "mesh": {"pointCount": len(result["nativeMesh"]["points"]),
                               "triangleCount": len(result["nativeMesh"]["triangles"]),
                               "bounds": result["nativeMesh"]["bounds"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
