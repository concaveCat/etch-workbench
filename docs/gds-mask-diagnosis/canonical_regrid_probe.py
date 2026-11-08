"""Prove mask reconstruction on the unchanged user grid using native APIs only."""
import argparse
import json
from pathlib import Path

import viennaps as ps


def stats(domain):
    mesh = domain.getSurfaceMesh(addInterfaces=True)
    points, cells = mesh.getNodes(), mesh.getTriangles()
    data = mesh.getCellData()
    ids = next(list(data.getScalarData(i)) for i in range(data.getScalarDataSize())
               if data.getScalarDataLabel(i) == "MaterialIds")
    by_material = {}
    for material in sorted(set(map(int, ids))):
        vertices = {v for cell, mid in zip(cells, ids) if int(mid) == material for v in cell}
        by_material[str(material)] = {
            "triangles": sum(int(mid) == material for mid in ids),
            "bounds": [fn(points[v][a] for v in vertices) for a in range(3) for fn in (min, max)],
        }
    samples = {}
    for x, y in [(0., 0.), (-.2, .1), (-1.5, 1.), (.75, .1), (2.35, .1), (-2.35, .1), (-1.6, .1)]:
        heights = []
        for cell, mid in zip(cells, ids):
            if int(mid) != 30:
                continue
            a, b, c = [points[v] for v in cell]
            denominator = (b[1]-c[1])*(a[0]-c[0]) + (c[0]-b[0])*(a[1]-c[1])
            if abs(denominator) < 1e-12:
                continue
            u = ((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/denominator
            v = ((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/denominator
            if min(u, v, 1-u-v) >= -1e-8:
                heights.append(u*a[2]+v*b[2]+(1-u-v)*c[2])
        samples[f"{x},{y}"] = max(heights) if heights else None
    return {"points": len(points), "triangles": len(cells), "materials": by_material,
            "filmHeights": samples,
            "levelSetPoints": [level.getNumberOfPoints() for level in domain.getLevelSets()]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=["gds", "box", "crossing", "outside", "full", "ring"])
    parser.add_argument("gds", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--boundary", choices=["reflective", "periodic"], default="reflective")
    parser.add_argument("--retained", choices=["inside", "outside"], default="inside")
    parser.add_argument("--delta", type=float, default=0.1)
    parser.add_argument("--duration", type=float, default=0.2)
    parser.add_argument("--extent", type=float, default=5.0)
    args = parser.parse_args()
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    lateral = (ps.BoundaryType.REFLECTIVE_BOUNDARY if args.boundary == "reflective"
               else ps.BoundaryType.PERIODIC_BOUNDARY)
    reflective = ps.BoundaryType.REFLECTIVE_BOUNDARY
    delta = args.delta
    domain = ps.Domain(delta, args.extent, args.extent, lateral)
    ps.MakePlane(domain, height=0., material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=5., material=ps.Material.SiO2, addToExisting=True).apply()
    report = {"args": vars(args) | {"gds": str(args.gds), "output": str(args.output)},
              "setupBefore": list(domain.getSetup().bounds())}
    if args.source == "gds":
        geometry = ps.d2.GDSGeometry(delta)
        geometry.setBoundaryConditions([reflective, reflective])
        ps.d2.GDSReader(geometry, str(args.gds)).apply()
        planar = geometry.layerToLevelSet(68, True)
    else:
        planar = ps.ls.d2.Domain([-4., 4., -4., 4.], [reflective, reflective], delta)
        corners = {"box": ([-.5, -.5], [.5, .5]),
                   "crossing": ([2.2, -.5], [2.8, .5]),
                   "outside": ([3., -.5], [3.8, .5]),
                   "full": ([-3., -3.], [3., 3.]),
                   "ring": ([-1., -1.], [1., 1.])}[args.source]
        ps.ls.d2.MakeGeometry(planar, ps.ls.d2.Box(*corners)).apply()
        if args.source == "ring":
            cut = ps.ls.d2.Domain([-4., 4., -4., 4.], [reflective, reflective], delta)
            ps.ls.d2.MakeGeometry(cut, ps.ls.d2.Box([-.5, -.5], [.5, .5])).apply()
            ps.ls.d2.BooleanOperation(planar, cut, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    # Always extrude the polygon interior, never its infinite planar complement.
    extruded = ps.ls.d3.Domain(delta)
    extrusion = ps.ls.Extrude(planar, extruded, [5., 5.3], 2,
                            [reflective, reflective, ps.BoundaryType.INFINITE_BOUNDARY])
    extrusion.apply()
    # Enforce finite horizontal caps on the entire polygon interior, including
    # long negative runs far away from the contour in the sparse planar field.
    source_upper = ps.ls.d3.Domain(extruded)
    source_lower = ps.ls.d3.Domain(extruded)
    ps.ls.d3.MakeGeometry(source_upper, ps.ls.d3.Plane([0., 0., 5.3], [0., 0., 1.])).apply()
    ps.ls.d3.MakeGeometry(source_lower, ps.ls.d3.Plane([0., 0., 5.], [0., 0., 1.])).apply()
    ps.ls.d3.BooleanOperation(source_upper, source_lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    ps.ls.d3.BooleanOperation(extruded, source_upper, ps.ls.BooleanOperationEnum.INTERSECT).apply()
    source_mesh = ps.ls.Mesh()
    ps.ls.d3.ToSurfaceMesh(ps.ls.d3.Domain(extruded), source_mesh, 0.01).apply()
    target = ps.ls.d3.Domain(domain.getGrid())
    reconstruction = ps.ls.d3.FromSurfaceMesh(target, source_mesh)
    reconstruction.setRemoveBoundaryTriangles(False)
    reconstruction.apply()
    report["insideTargetPoints"] = target.getNumberOfPoints()
    if args.retained == "outside":
        factory = ps.GeometryFactory(domain.getSetup(), "finite-complement")
        slab = factory.makeSubstrate(5.3)
        lower = factory.makeSubstrate(5.)
        ps.ls.d3.BooleanOperation(slab, lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        if report["insideTargetPoints"]:
            ps.ls.d3.BooleanOperation(slab, target, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        target = slab
    report["targetPoints"] = target.getNumberOfPoints()
    print("reconstructed", report["targetPoints"], flush=True)
    if report["targetPoints"] == 0:
        report["emptyInDomain"] = True
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return
    domain.insertNextLevelSetAsMaterial(target, ps.Material.Mask, True)
    report["before"] = stats(domain)
    print("before", report["before"], flush=True)
    process = ps.Process(domain, ps.IsotropicProcess(rate=-.2, maskMaterials=[ps.Material.Mask]), args.duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    report["after"] = stats(domain)
    report["setupAfter"] = list(domain.getSetup().bounds())
    assert report["setupBefore"] == report["setupAfter"]
    assert all(report["after"]["levelSetPoints"])
    assert {"0", "10", "30"} <= set(report["after"]["materials"])
    print("after", report["after"], flush=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
