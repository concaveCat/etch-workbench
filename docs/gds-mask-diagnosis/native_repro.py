"""Isolated native ViennaPS 4.6.2 reproduction; no Workbench imports.

Run each mode in its own process; native failures must not stop the matrix.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import viennaps as ps


def mesh_stats(domain):
    mesh = domain.getSurfaceMesh(addInterfaces=True)
    points = mesh.getNodes()
    data = mesh.getCellData()
    ids = []
    for index in range(data.getScalarDataSize()):
        if data.getScalarDataLabel(index) == "MaterialIds":
            ids = list(data.getScalarData(index))
    return {
        "points": len(points), "triangles": len(mesh.getTriangles()),
        "materialTriangles": dict(Counter(int(value) for value in ids)),
        "bounds": [fn(point[axis] for point in points)
                   for axis in range(3) for fn in (min, max)] if points else [],
        "levelSetPoints": [level.getNumberOfPoints() for level in domain.getLevelSets()],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["box-unwrapped", "box-wrapped", "box-mismatched-wrapped",
                        "gds-unwrapped", "gds-wrapped", "gds-matched-wrapped"])
    parser.add_argument("gds", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta = 0.1
    lateral = ps.BoundaryType.REFLECTIVE_BOUNDARY
    bc = [lateral, lateral, ps.BoundaryType.INFINITE_BOUNDARY]
    domain = ps.Domain(delta, 5.0, 5.0, lateral)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=5.0, material=ps.Material.SiO2, addToExisting=True).apply()
    report = {"mode": args.mode}
    if args.mode.startswith("box"):
        mask_domain = (ps.Domain(delta, 6.0, 6.0, lateral)
                       if args.mode == "box-mismatched-wrapped" else domain)
        mask = ps.GeometryFactory(mask_domain.getSetup(), "diagnostic").makeBoxStencil(
            [-0.5, 0.0, 5.0], 1.0, 0.3, 0.0, 1.0)
    else:
        geometry = ps.d2.GDSGeometry(delta)
        geometry.setBoundaryConditions([lateral, lateral])
        ps.d2.GDSReader(geometry, str(args.gds)).apply()
        report["gdsBounds"] = list(geometry.getBounds())
        print("GDS bounds", report["gdsBounds"], flush=True)
        planar = geometry.layerToLevelSet(68, True)
        mask = ps.ls.d3.Domain(delta)
        extrusion = ps.ls.Extrude()
        extrusion.setInputLevelSet(planar)
        extrusion.setOutputLevelSet(mask)
        extrusion.setExtent([5.0, 5.3])
        extrusion.setExtrusionAxis(2)
        extrusion.setBoundaryConditions(bc)
        extrusion.apply()
        if args.mode == "gds-matched-wrapped":
            # Make both lower interfaces on the exact grid inherited by mask.
            domain = ps.Domain()
            for height, material in [(0.0, ps.Material.Si), (5.0, ps.Material.SiO2)]:
                plane = ps.ls.d3.Domain(mask)
                ps.ls.d3.MakeGeometry(plane, ps.ls.d3.Plane([0., 0., height], [0., 0., 1.])).apply()
                domain.insertNextLevelSetAsMaterial(plane, material)
    print("inserting mask", flush=True)
    domain.insertNextLevelSetAsMaterial(mask, ps.Material.Mask, not args.mode.endswith("unwrapped"))
    print("mask inserted", flush=True)
    report["before"] = mesh_stats(domain)
    print("before", report["before"], flush=True)
    process = ps.Process(domain, ps.IsotropicProcess(rate=-0.2, maskMaterials=[ps.Material.Mask]), 0.01)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    report["after"] = mesh_stats(domain)
    print("after", report["after"], flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
