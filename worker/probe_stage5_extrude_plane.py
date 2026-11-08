"""Isolate ViennaPS Extrude on a native 2D plane; run via bounded_child only."""
import json
import sys
from pathlib import Path

import viennaps as ps


def main():
    delta, x_extent, y_extent = 0.2, 4.0, 2.0
    input_domain = ps.d2.Domain(delta, x_extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.d2.MakePlane(input_domain, height=1.0, material=ps.Material.Si, addToExisting=False).apply()
    output_domain = ps.d3.Domain(delta, x_extent, y_extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    operation = ps.Extrude()
    operation.setInputDomain(input_domain)
    operation.setOutputDomain(output_domain)
    operation.setExtent([-1.0, 1.0])
    operation.setExtrusionAxis(1)
    operation.setBoundaryConditions([ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                     ps.BoundaryType.REFLECTIVE_BOUNDARY,
                                     ps.BoundaryType.INFINITE_BOUNDARY])
    operation.apply()
    mesh = output_domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    report = {"inputBounds": input_domain.getBoundingBox(), "outputBounds": output_domain.getBoundingBox(),
              "pointBounds": [[min(row[i] for row in points), max(row[i] for row in points)] for i in range(3)] if points else None,
              "pointCount": len(points), "triangles": len(mesh.getTriangles()),
              "levelSetCount": output_domain.getNumberOfLevelSets()}
    path = Path(sys.argv[1]).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
