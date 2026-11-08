"""Bounded ViennaPS probe for DirectionalProcess signs and coordinate axes."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import viennaps as ps


def summary(domain, dimension: int) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(v) for v in point] for point in mesh.getNodes()]
    vertical = 1 if dimension == 2 else 2
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = sorted({int(v) for v in data.getScalarData(index)})
    return {
        "pointCount": len(points),
        "medianHeight": statistics.median(point[vertical] for point in points),
        "minHeight": min(point[vertical] for point in points),
        "maxHeight": max(point[vertical] for point in points),
        "materialIds": material_ids,
        "bounds": [[min(p[a] for p in points), max(p[a] for p in points)] for a in range(3)],
    }


def make_flat(dimension: int):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    delta = 0.05
    if dimension == 2:
        domain = ps.Domain(delta, 3.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(delta, 2.0, 2.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    return domain


def make_trench(dimension: int):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    delta = 0.05
    if dimension == 2:
        domain = ps.Domain(delta, 3.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(delta, 2.0, 2.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakeTrench(domain=domain, trenchWidth=0.8, trenchDepth=0.8,
                  trenchTaperAngle=0.0, maskHeight=0.3, maskTaperAngle=0.0,
                  halfTrench=False, material=ps.Material.Si,
                  maskMaterial=ps.Material.Mask).apply()
    return domain


def apply(domain, dimension: int, direction, directional_velocity: float,
          isotropic_velocity: float, visibility: bool, duration: float = 0.5,
          mask_materials=None):
    # The installed 4.6.2 overload calls its fourth positional argument
    # ``maskMaterial`` even when that argument is a sequence. The stubs do not
    # accept the spelling ``maskMaterials`` as a keyword.
    model = ps.DirectionalProcess(
        direction, directional_velocity, isotropic_velocity,
        [ps.Material.Mask] if mask_materials is None else mask_materials, visibility,
    )
    process = ps.Process(domain, model, duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    process.apply()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for dimension, vertical in ((2, 1), (3, 2)):
        up = [0.0, 1.0, 0.0] if dimension == 2 else [0.0, 0.0, 1.0]
        down = [-v for v in up]
        cases = [
            ("direction-up-positive", up, 0.5, 0.0, True),
            ("direction-down-positive", down, 0.5, 0.0, True),
            ("direction-up-negative", up, -0.5, 0.0, True),
            ("direction-down-negative", down, -0.5, 0.0, True),
            ("isotropic-positive", up, 0.0, 0.5, True),
            ("isotropic-negative", up, 0.0, -0.5, True),
        ]
        for label, direction, directional_velocity, isotropic_velocity, visibility in cases:
            domain = make_flat(dimension)
            before = summary(domain, dimension)
            apply(domain, dimension, direction, directional_velocity, isotropic_velocity, visibility,
                  mask_materials=[])
            after = summary(domain, dimension)
            rows.append({
                "dimension": dimension,
                "case": label,
                "direction": direction,
                "directionalVelocity": directional_velocity,
                "isotropicVelocity": isotropic_velocity,
                "calculateVisibility": visibility,
                "duration": 0.5,
                "before": before,
                "after": after,
                "heightDelta": after["medianHeight"] - before["medianHeight"],
            })

        # Compare visibility on a real trench. A diagonal beam shadows one wall;
        # the unshadowed run should therefore produce a different true domain.
        direction = [0.6, -0.8, 0.0] if dimension == 2 else [0.6, 0.0, -0.8]
        # The negative native sign is paired with the incoming/downward beam and
        # will be reinterpreted after the flat-interface sign cases above.
        for visibility in (True, False):
            domain = make_trench(dimension)
            before = summary(domain, dimension)
            apply(domain, dimension, direction, -0.5, 0.0, visibility, duration=0.4,
                  mask_materials=[ps.Material.Mask])
            after = summary(domain, dimension)
            rows.append({
                "dimension": dimension,
                "case": "trench-visibility-on" if visibility else "trench-visibility-off",
                "direction": direction,
                "directionalVelocity": -0.5,
                "isotropicVelocity": 0.0,
                "calculateVisibility": visibility,
                "duration": 0.4,
                "before": before,
                "after": after,
            })

    import importlib.metadata
    result = {
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "coordinateConvention": {"2D": "[x, h, 0]", "3D": "[x, y, z], z is vertical"},
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"versions": result["versions"], "rowCount": len(rows),
                      "heightDeltas": [{"dimension": r["dimension"], "case": r["case"], "heightDelta": r.get("heightDelta")} for r in rows if "heightDelta" in r]}, indent=2))


if __name__ == "__main__":
    main()
