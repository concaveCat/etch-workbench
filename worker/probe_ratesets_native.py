"""Bounded ViennaPS probe for the native multi-``RateSet`` DirectionalProcess.

Run through ``scripts/bounded_child.py`` or directly with the workbench venv:

    .venv/bin/python worker/probe_ratesets_native.py --output <path.json>

It establishes, against the installed bindings only:

* ``DirectionalProcess(Sequence[RateSet])`` is accepted in 2D and 3D;
* directional and isotropic velocities are additive across sets;
* opposite directions cancel;
* ``maskMaterials`` is applied per set, not globally;
* ``calculateVisibility`` is per set;
* a RateSet with zero directional velocity reports ``calculateVisibility``
  as false (the native constructor ANDs it with a nonzero direction/velocity).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

import viennaps as ps


def set_dimension(dimension: int) -> None:
    ps.setDimension(dimension)
    ps.setNumThreads(1)


def plane(dimension: int, material=ps.Material.Si, extent: float = 2.0):
    set_dimension(dimension)
    delta = 0.05
    if dimension == 2:
        domain = ps.Domain(delta, extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(delta, extent, extent, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=material, addToExisting=False).apply()
    return domain


def trench(dimension: int):
    set_dimension(dimension)
    delta = 0.05
    if dimension == 2:
        domain = ps.Domain(delta, 3.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(delta, 2.0, 2.0, ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakeTrench(domain=domain, trenchWidth=0.8, trenchDepth=0.8, trenchTaperAngle=0.0,
                  maskHeight=0.3, maskTaperAngle=0.0, halfTrench=False,
                  material=ps.Material.Si, maskMaterial=ps.Material.Mask).apply()
    return domain


def heights(domain, dimension: int) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    vertical = 1 if dimension == 2 else 2
    return {
        "pointCount": len(points),
        "medianHeight": statistics.median(point[vertical] for point in points),
        "minHeight": min(point[vertical] for point in points),
        "maxHeight": max(point[vertical] for point in points),
    }


def apply(domain, rate_sets, duration: float = 0.2) -> None:
    process = ps.Process(domain, ps.DirectionalProcess(rate_sets), duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    process.apply()


def rate_set(direction, directional_velocity, isotropic_velocity=0.0, masks=None,
             visibility: bool = True):
    return ps.RateSet(list(direction), directional_velocity, isotropic_velocity,
                      [] if masks is None else masks, visibility)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows: list[dict] = []

    for dimension, up in ((2, [0.0, 1.0, 0.0]), (3, [0.0, 0.0, 1.0])):
        down = [-value for value in up]

        def run(sets, label, duration=0.2, material=ps.Material.Si):
            domain = plane(dimension, material)
            before = heights(domain, dimension)
            apply(domain, sets, duration)
            after = heights(domain, dimension)
            rows.append({"dimension": dimension, "case": label, "before": before, "after": after,
                         "heightDelta": after["medianHeight"] - before["medianHeight"]})
            return {**after, "initialMedianHeight": before["medianHeight"]}

        single = run([rate_set(up, 0.3, masks=[])], "single-0.3")
        split = run([rate_set(up, 0.15, masks=[]), rate_set(up, 0.15, masks=[])], "two-0.15+0.15")
        opposite = run([rate_set(up, 0.15, masks=[]), rate_set(down, 0.15, masks=[])],
                       "opposite-0.15+0.15")
        isotropic_single = run([rate_set([0.0, 0.0, 0.0], 0.0, 0.05, masks=[])], "isotropic-single-0.05")
        isotropic_split = run([rate_set([0.0, 0.0, 0.0], 0.0, 0.025, masks=[]),
                               rate_set([0.0, 0.0, 0.0], 0.0, 0.025, masks=[])],
                              "isotropic-two-0.025+0.025")
        rows.append({
            "dimension": dimension,
            "case": "additivity",
            "directionalSplitEqualsSingle": abs(split["medianHeight"] - single["medianHeight"]) <= 1e-12,
            "isotropicSplitEqualsSingle": abs(isotropic_split["medianHeight"] - isotropic_single["medianHeight"]) <= 1e-12,
            "oppositeIsStatic": abs(opposite["medianHeight"] - opposite["initialMedianHeight"]) <= 1e-9,
        })

        mask_idx = ps.Material.Mask
        masked = run([rate_set(up, 0.3, masks=[mask_idx])], "mask-plane-set-masked",
                     material=ps.Material.Mask)
        unmasked = run([rate_set(up, 0.3, masks=[])], "mask-plane-set-unmasked",
                       material=ps.Material.Mask)
        mixed = run([rate_set(up, 0.3, masks=[mask_idx]), rate_set(up, 0.3, masks=[])],
                    "mask-plane-mixed", material=ps.Material.Mask)
        rows.append({"dimension": dimension, "case": "per-set-mask",
                     "maskedIsStatic": abs(masked["medianHeight"] - masked["initialMedianHeight"]) <= 1e-9,
                     "unmaskedMoves": unmasked["medianHeight"] < masked["medianHeight"] - 1e-6,
                     "mixedMovesLikeUnmasked": abs(mixed["medianHeight"] - unmasked["medianHeight"]) <= 1e-12})

        diagonal = [0.6, -0.8, 0.0] if dimension == 2 else [0.6, 0.0, -0.8]
        visibility_heights = {}
        for label, visibility in (("visibility-both", (True, True)),
                                  ("visibility-none", (False, False)),
                                  ("visibility-mixed", (True, False))):
            domain = trench(dimension)
            apply(domain, [rate_set(diagonal, -0.3, masks=[ps.Material.Mask], visibility=visibility[0]),
                           rate_set(diagonal, -0.3, masks=[ps.Material.Mask], visibility=visibility[1])],
                  duration=0.3)
            visibility_heights[label] = heights(domain, dimension)["minHeight"]
            rows.append({"dimension": dimension, "case": label, "minHeight": visibility_heights[label]})
        rows.append({"dimension": dimension, "case": "visibility-distinct",
                     "distinct": len({round(value, 12) for value in visibility_heights.values()}) == 3})

        zero_directional = ps.RateSet(up, 0.0, 0.2, [], True)
        rows.append({"dimension": dimension, "case": "zero-directional-visibility",
                     "requested": True, "native": bool(zero_directional.calculateVisibility)})

    import importlib.metadata
    result = {
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "coordinateConvention": {"2D": "[x, h, 0]", "3D": "[x, y, z], z is vertical"},
        "positiveDirectionalVelocityMovesAgainstDirection": True,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    summary = {row["case"]: {key: value for key, value in row.items()
                             if key in {"heightDelta", "directionalSplitEqualsSingle",
                                        "isotropicSplitEqualsSingle", "oppositeIsStatic",
                                        "maskedIsStatic", "unmaskedMoves", "mixedMovesLikeUnmasked",
                                        "distinct", "native", "minHeight", "dimension"}}
               for row in rows}
    print(json.dumps({"versions": result["versions"], "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
