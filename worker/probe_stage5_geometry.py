"""Exercise installed ViennaPS Fin, Extrude, and Slice geometry bindings.

Run this native probe only through scripts/bounded_child.py.
"""
from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import viennaps as ps


def mesh_report(mesh):
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    topology = {"lines": [[int(value) for value in line] for line in mesh.getLines()],
                "triangles": [[int(value) for value in row] for row in mesh.getTriangles()]}
    topology = {name: cells for name, cells in topology.items() if cells}
    cell_data = mesh.getCellData()
    scalars = {str(cell_data.getScalarDataLabel(index)): [int(value) for value in cell_data.getScalarData(index)]
               for index in range(cell_data.getScalarDataSize())}
    return {"points": points, "topology": topology, "cellScalars": scalars}


def surface(domain):
    return mesh_report(domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01))


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    delta, x_extent, y_extent = 0.2, 4.0, 2.0
    axis = int(os.environ.get("STAGE5_EXTRUSION_AXIS", "1"))
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    report = {"versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
              "setup": {"gridDelta": delta, "xExtent": x_extent, "extrusionExtent": [-1.0, 1.0], "extrusionAxis": axis},
              "fin2d": {}, "fin3d": {}, "extruded": {}, "slice": {}}

    print("probe make fin 2d", flush=True)
    ps.setDimension(2)
    domain2 = ps.d2.Domain(delta, x_extent, boundary)
    ps.d2.MakeFin(domain2, finWidth=0.8, finHeight=1.2, finTaperAngle=5.0,
               maskHeight=0.4, maskTaperAngle=0.0, halfFin=False,
               material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
    print("probe mesh fin 2d", flush=True)
    report["fin2d"] = {"mesh": surface(domain2), "levelSetCount": int(domain2.getNumberOfLevelSets()),
                       "materialMap": [int(domain2.getMaterialMap().getMaterialIdAtIdx(index))
                                       for index in range(domain2.getMaterialMap().size())]}

    print("probe make fin 3d", flush=True)
    ps.setDimension(3)
    domain3_fin = ps.d3.Domain(delta, x_extent, y_extent, boundary)
    ps.d3.MakeFin(domain3_fin, finWidth=0.8, finHeight=1.2, finTaperAngle=5.0,
               maskHeight=0.4, maskTaperAngle=0.0, halfFin=False,
               material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
    print("probe mesh fin 3d", flush=True)
    report["fin3d"] = {"mesh": surface(domain3_fin), "levelSetCount": int(domain3_fin.getNumberOfLevelSets()),
                       "materialMap": [int(domain3_fin.getMaterialMap().getMaterialIdAtIdx(index))
                                       for index in range(domain3_fin.getMaterialMap().size())]}

    print("probe extrude", flush=True)
    ps.setDimension(3)
    domain3_extruded = ps.d3.Domain(delta, x_extent, y_extent, boundary)
    extrude = ps.Extrude()
    extrude.setInputDomain(domain2)
    extrude.setOutputDomain(domain3_extruded)
    extrude.setExtent([-1.0, 1.0])
    extrude.setExtrusionAxis(axis)
    extrude.setBoundaryConditions([boundary, boundary, ps.BoundaryType.INFINITE_BOUNDARY])
    print("probe extrude apply", flush=True)
    extrude.apply()
    print("probe mesh extruded", flush=True)
    report["extruded"] = {"mesh": surface(domain3_extruded),
                          "levelSetCount": int(domain3_extruded.getNumberOfLevelSets()),
                          "materialMap": [int(domain3_extruded.getMaterialMap().getMaterialIdAtIdx(index))
                                          for index in range(domain3_extruded.getMaterialMap().size())]}

    if "--no-slice" in sys.argv[2:]:
        destination = output / "geometry-native-probe.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(destination),
                          "counts": {name: {"points": len(value["mesh"]["points"]),
                                             "cells": sum(len(cells) for cells in value["mesh"]["topology"].values())}
                                     for name, value in report.items() if name in ("fin2d", "fin3d", "extruded")}},
                         ensure_ascii=False))
        return

    slice_dimension = int(os.environ.get("STAGE5_SLICE_DIMENSION", "1"))
    slice_position = float(os.environ.get("STAGE5_SLICE_POSITION", "0"))
    slice_source = domain3_fin if os.environ.get("STAGE5_SLICE_INPUT") == "fin" else domain3_extruded
    print("probe slice source bounds", slice_source.getBoundingBox(), flush=True)
    print("probe slice", flush=True)
    ps.setDimension(2)
    sliced = ps.d2.Domain(delta, x_extent, boundary)
    slicer = ps.Slice(slice_source, sliced, slice_dimension, slice_position)
    slicer.apply()
    print("probe slice level sets", sliced.getNumberOfLevelSets(), flush=True)
    if sliced.getNumberOfLevelSets() == 0:
        report["slice"] = {"status": "native Slice returned an empty Domain"}
        destination = output / "geometry-native-probe.json"
        destination.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(destination), "slice": report["slice"]}, ensure_ascii=False))
        return
    print("probe mesh slice", flush=True)
    report["slice"] = {"mesh": surface(sliced), "levelSetCount": int(sliced.getNumberOfLevelSets()),
                       "materialMap": [int(sliced.getMaterialMap().getMaterialIdAtIdx(index))
                                       for index in range(sliced.getMaterialMap().size())]}
    destination = output / "geometry-native-probe.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(destination),
                      "counts": {name: {"points": len(value["mesh"]["points"]),
                                         "cells": sum(len(cells) for cells in value["mesh"]["topology"].values())}
                                 for name, value in report.items() if name in ("fin2d", "fin3d", "extruded", "slice")}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
