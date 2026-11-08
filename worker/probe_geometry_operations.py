"""Native ViennaPS 4.6.2 geometry-operation probe; run only via bounded_child.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps


def make_stack(dimension: int):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    if dimension == 2:
        domain = ps.Domain(0.2, 4.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(0.2, 4.0, 4.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=0.4, material=ps.Material.SiO2, addToExisting=True).apply()
    ps.MakePlane(domain, height=0.8, material=ps.Material.Si3N4, addToExisting=True).apply()
    return domain


def summary(domain: object) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = mesh.getNodes()
    cells = mesh.getTriangles() or mesh.getLines()
    cell_data = mesh.getCellData()
    materials = []
    for index in range(cell_data.getScalarDataSize()):
        if str(cell_data.getScalarDataLabel(index)) == "MaterialIds":
            materials = [int(value) for value in cell_data.getScalarData(index)]
    return {
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "materialMap": [int(domain.getMaterialMap().getMaterialIdAtIdx(i)) for i in range(domain.getMaterialMap().size())],
        "pointCount": len(points),
        "cellCount": len(cells),
        "cellMaterialIds": sorted(set(materials)),
        "boundingBox": domain.getBoundingBox(),
    }


def main() -> None:
    target = Path(sys.argv[1])
    output = {"versions": {"viennaps": ps.version, "viennals": ps.ls.version}, "dimensions": {}}
    for dimension in (2, 3):
        base = make_stack(dimension)
        cases = {"before": summary(base)}

        removed = ps.Domain(base)
        removed.removeMaterial(ps.Material.SiO2)
        cases["removeSiO2"] = summary(removed)

        flattened = ps.Domain(base)
        planarize = ps.Planarize(flattened, 0.5)
        planarize.apply()
        cases["planarizeCutoff0_5"] = summary(flattened)

        factory = ps.GeometryFactory(base.getSetup(), "GeometryOperationProbe")
        if dimension == 2:
            operand = factory.makeBoxStencil([0.0, 0.3], 1.2, 0.8, 0.0)
        else:
            operand = factory.makeBoxStencil([0.0, 0.0, 0.3], 1.2, 0.8, 0.0, 1.2)
        operand_mesh = ps.ls.Mesh()
        ps.ls.ToMesh(operand, operand_mesh).apply()
        operand_points = operand_mesh.getNodes()
        operand_bounds = [
            [min(point[axis] for point in operand_points), max(point[axis] for point in operand_points)]
            for axis in range(dimension)
        ]
        cases["booleanOperand"] = {
            "pointCount": int(operand.getNumberOfPoints()),
            "levelSetWidth": int(operand.getLevelSetWidth()),
            "surfacePointCount": len(operand_points),
            "surfaceBounds": operand_bounds,
        }
        for name in ("UNION", "INTERSECT", "RELATIVE_COMPLEMENT", "INVERT"):
            combined = ps.Domain(base)
            combined.applyBooleanOperation(operand, getattr(ps.ls.BooleanOperationEnum, name), True)
            cases[f"boolean{name}"] = summary(combined)
        output["dimensions"][str(dimension)] = cases

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"versions": output["versions"], "dimensions": sorted(output["dimensions"])}))


if __name__ == "__main__":
    main()
