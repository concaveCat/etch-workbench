"""Exercise the product GDS stack-mask helper in an isolated native process.

This fixture intentionally imports ``worker.gds_mask`` (the shipped code path)
rather than re-implementing the construction.  It must be launched as a child of
``scripts/bounded_child.py``; the parent test reads the JSON report and checks
geometry/physics invariants instead of trusting the exit status.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import viennaps as ps  # noqa: E402

from worker.gds_mask import build_aligned_gds_mask  # noqa: E402

SAMPLE_POINTS = [(0.0, 0.0), (-0.2, 0.1), (-1.5, 1.0), (0.75, 0.1),
                 (2.35, 0.1), (-2.35, 0.1), (-1.6, 0.1)]
SIO2 = 30


def _setup_snapshot(domain):
    setup = domain.getSetup()
    return {"bounds": [float(value) for value in setup.bounds()],
            "gridDelta": float(setup.gridDelta()),
            "boundary": [str(value) for value in setup.boundaryCons()]}


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
    heights = {}
    for x, y in SAMPLE_POINTS:
        candidates = []
        for cell, mid in zip(cells, ids):
            if int(mid) != SIO2:
                continue
            a, b, c = [points[v] for v in cell]
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(denominator) < 1e-12:
                continue
            u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
            v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
            if min(u, v, 1 - u - v) >= -1e-8:
                candidates.append(u * a[2] + v * b[2] + (1 - u - v) * c[2])
        heights[f"{x},{y}"] = max(candidates) if candidates else None
    return {"points": len(points), "triangles": len(cells), "materials": by_material,
            "filmHeights": heights,
            "levelSetPoints": [level.getNumberOfPoints() for level in domain.getLevelSets()]}


def planar_shape(name, delta, reflective):
    if name == "gds":
        raise AssertionError("gds source needs a file")
    planar = ps.ls.d2.Domain([-4.0, 4.0, -4.0, 4.0], [reflective, reflective], delta)
    corners = {"box": ([-0.5, -0.5], [0.5, 0.5]),
               "crossing": ([2.2, -0.5], [2.8, 0.5]),
               "outside": ([3.0, -0.5], [3.8, 0.5]),
               "full": ([-3.0, -3.0], [3.0, 3.0]),
               "ring": ([-1.0, -1.0], [1.0, 1.0])}[name]
    ps.ls.d2.MakeGeometry(planar, ps.ls.d2.Box(*corners)).apply()
    if name == "ring":
        cut = ps.ls.d2.Domain([-4.0, 4.0, -4.0, 4.0], [reflective, reflective], delta)
        ps.ls.d2.MakeGeometry(cut, ps.ls.d2.Box([-0.5, -0.5], [0.5, 0.5])).apply()
        ps.ls.d2.BooleanOperation(
            planar, cut, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    return planar


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
    parser.add_argument("--base-height", type=float, default=5.0)
    parser.add_argument("--mask-height", type=float, default=0.3)
    parser.add_argument("--rate", type=float, default=-0.2)
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
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    ps.MakePlane(domain, height=args.base_height, material=ps.Material.SiO2,
                 addToExisting=True).apply()
    report = {"args": {key: (str(value) if isinstance(value, Path) else value)
                       for key, value in vars(args).items()},
              "setupBefore": _setup_snapshot(domain)}

    if args.source == "gds":
        geometry = ps.d2.GDSGeometry(delta)
        geometry.setBoundaryConditions([reflective, reflective])
        ps.d2.GDSReader(geometry, str(args.gds)).apply()
        planar = geometry.layerToLevelSet(68, True)
    else:
        planar = planar_shape(args.source, delta, reflective)

    try:
        target, diagnostics = build_aligned_gds_mask(
            ps, domain, planar, base_height=args.base_height,
            height=args.mask_height, retained_region=args.retained)
    except RuntimeError as exc:
        report["emptyError"] = str(exc)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        return 0
    report["diagnostics"] = diagnostics
    report["maskPoints"] = int(target.getNumberOfPoints())
    domain.insertNextLevelSetAsMaterial(target, ps.Material.Mask, True)
    report["before"] = stats(domain)
    process = ps.Process(domain, ps.IsotropicProcess(
        rate=args.rate, maskMaterials=[ps.Material.Mask]), args.duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    report["after"] = stats(domain)
    report["setupAfter"] = _setup_snapshot(domain)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
