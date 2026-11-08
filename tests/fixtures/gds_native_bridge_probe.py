"""Compare native GDS bodies against a direct ViennaLS reference.

Run only through ``scripts/bounded_child.py``. The reference builds the same
box with ``ls.MakeGeometry``/``ls.Extrude``/plane intersections; it does not
import the product body helpers, so this is an independent geometry check.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
SURFACE_TOLERANCE = 0.01


def surface_bounds(level_set, dimension):
    mesh = ps.ls.Mesh()
    if dimension == 2:
        ps.ls.d2.ToSurfaceMesh(level_set, mesh).apply()
    else:
        ps.ls.d3.ToSurfaceMesh(level_set, mesh, 0.001).apply()
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    if not points:
        return None
    return [[min(point[axis] for point in points), max(point[axis] for point in points)]
            for axis in range(dimension)]


def capped_extrusion(planar, base, top, boundaries):
    body = ps.ls.d3.Domain(0.1)
    ps.ls.Extrude(planar, body, [base - 0.1, top + 0.1], 2, boundaries).apply()
    upper = ps.ls.d3.Domain(body)
    lower = ps.ls.d3.Domain(body)
    ps.ls.d3.MakeGeometry(upper, ps.ls.d3.Plane([0.0, 0.0, top], [0.0, 0.0, 1.0])).apply()
    ps.ls.d3.MakeGeometry(lower, ps.ls.d3.Plane([0.0, 0.0, base], [0.0, 0.0, 1.0])).apply()
    ps.ls.d3.BooleanOperation(
        upper, lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    ps.ls.d3.BooleanOperation(body, upper, ps.ls.BooleanOperationEnum.INTERSECT).apply()
    return body


def main() -> None:
    gds_path = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    ps.setDimension(3)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    reflective = ps.BoundaryType.REFLECTIVE_BOUNDARY
    infinite = ps.BoundaryType.INFINITE_BOUNDARY
    delta = 0.1
    padding = 0.2
    report = {"delta": delta, "padding": padding, "checks": {}}

    # 2D native layer 1 is the 3 x 1 um rectangle of multi-layer.gds.
    geometry2 = ps.d2.GDSGeometry(delta)
    geometry2.setBoundaryConditions([reflective, reflective])
    geometry2.setBoundaryPadding(padding, padding)
    ps.d2.GDSReader(geometry2, str(gds_path)).apply()
    native2 = geometry2.layerToLevelSet(1, True)
    bounds2 = [float(value) for value in geometry2.getBounds()]
    reference2 = ps.ls.d2.Domain(bounds2[:4], [reflective, reflective], delta)
    ps.ls.d2.MakeGeometry(reference2, ps.ls.d2.Box([-1.5, -0.5], [1.5, 0.5])).apply()
    native2_bounds = surface_bounds(native2, 2)
    reference2_bounds = surface_bounds(reference2, 2)
    report["checks"]["2d"] = {
        "nativePoints": int(native2.getNumberOfPoints()),
        "referencePoints": int(reference2.getNumberOfPoints()),
        "nativeBounds": native2_bounds,
        "referenceBounds": reference2_bounds,
        "matches": bool(native2_bounds and reference2_bounds and all(
            abs(a - b) <= SURFACE_TOLERANCE
            for native_axis, reference_axis in zip(native2_bounds, reference2_bounds)
            for a, b in zip(native_axis, reference_axis))),
    }

    # 3D native layer 1 body at base 0.4 / height 0.4 (a measured native case
    # that returns a non-empty body in 4.6.2 when the frame is padded).
    geometry3 = ps.d3.GDSGeometry(delta)
    geometry3.setBoundaryConditions([reflective, reflective, infinite])
    geometry3.setBoundaryPadding(padding, padding)
    ps.d3.GDSReader(geometry3, str(gds_path)).apply()
    native3 = geometry3.layerToLevelSet(1, 0.4, 0.4, False, False)
    reference3 = capped_extrusion(
        reference2, 0.4, 0.8, [reflective, reflective, infinite])
    native3_bounds = surface_bounds(native3, 3)
    reference3_bounds = surface_bounds(reference3, 3)
    report["checks"]["3d"] = {
        "nativePoints": int(native3.getNumberOfPoints()),
        "referencePoints": int(reference3.getNumberOfPoints()),
        "nativeBounds": native3_bounds,
        "referenceBounds": reference3_bounds,
        "matches": bool(native3_bounds and reference3_bounds and all(
            abs(a - b) <= SURFACE_TOLERANCE
            for native_axis, reference_axis in zip(native3_bounds, reference3_bounds)
            for a, b in zip(native_axis, reference_axis))),
    }

    # Material mapping through the real native insertion path.
    domain = ps.Domain(delta, 6.0, 6.0, reflective)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si).apply()
    domain.insertNextLevelSetAsMaterial(native3, ps.Material.PolySi, True)
    mesh = domain.getSurfaceMesh(addInterfaces=True)
    data = mesh.getCellData()
    ids = next(list(data.getScalarData(index)) for index in range(data.getScalarDataSize())
               if data.getScalarDataLabel(index) == "MaterialIds")
    report["checks"]["materials"] = {
        "materialIds": sorted({int(value) for value in ids}),
        "levelSetPoints": [int(level.getNumberOfPoints()) for level in domain.getLevelSets()],
        "matches": {10, 11} <= {int(value) for value in ids},
    }
    report["ok"] = all(check.get("matches") for check in report["checks"].values())
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "output": str(output)}))


if __name__ == "__main__":
    main()
