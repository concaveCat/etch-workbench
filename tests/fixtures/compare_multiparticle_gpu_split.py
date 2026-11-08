"""Compare bounded CPU MultiParticle references with exploratory GPU splits.

This report generator never imports ViennaPS. Native work must already have
been run through scripts/bounded_child.py and is read from the saved artifacts.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


def guard_ok(path: Path) -> bool:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (row.get("returnCode") == 0 and not row.get("timedOut") and not row.get("rssLimited")
            and not row.get("logLimited") and not row.get("remainingOwnedGroupMembers"))


def point_segment_distance(point: list[float], start: list[float], end: list[float]) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    fraction = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared))
    return math.hypot(point[0] - start[0] - fraction * dx, point[1] - start[1] - fraction * dy)


def directed_mesh_distance(points: list[list[float]], other_points: list[list[float]],
                           cells: list[list[int]]) -> float:
    if not points or not other_points or not cells:
        return math.inf
    return max(min(point_segment_distance(point, other_points[a], other_points[b])
                   for a, b in cells) for point in points)


def point_triangle_distance(point: list[float], a: list[float], b: list[float], c: list[float]) -> float:
    """Euclidean distance to a triangle using the closest-region tests."""
    ab = [b[i] - a[i] for i in range(3)]
    ac = [c[i] - a[i] for i in range(3)]
    ap = [point[i] - a[i] for i in range(3)]
    d1 = sum(ab[i] * ap[i] for i in range(3))
    d2 = sum(ac[i] * ap[i] for i in range(3))
    if d1 <= 0 and d2 <= 0:
        return math.dist(point, a)
    bp = [point[i] - b[i] for i in range(3)]
    d3 = sum(ab[i] * bp[i] for i in range(3))
    d4 = sum(ac[i] * bp[i] for i in range(3))
    if d3 >= 0 and d4 <= d3:
        return math.dist(point, b)
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        v = d1 / (d1 - d3)
        return math.dist(point, [a[i] + v * ab[i] for i in range(3)])
    cp = [point[i] - c[i] for i in range(3)]
    d5 = sum(ab[i] * cp[i] for i in range(3))
    d6 = sum(ac[i] * cp[i] for i in range(3))
    if d6 >= 0 and d5 <= d6:
        return math.dist(point, c)
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        w = d2 / (d2 - d6)
        return math.dist(point, [a[i] + w * ac[i] for i in range(3)])
    va = d3 * d6 - d5 * d4
    if va <= 0 and d4 - d3 >= 0 and d5 - d6 >= 0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return math.dist(point, [b[i] + w * (c[i] - b[i]) for i in range(3)])
    denominator = 1.0 / (va + vb + vc)
    v, w = vb * denominator, vc * denominator
    closest = [a[i] + ab[i] * v + ac[i] * w for i in range(3)]
    return math.dist(point, closest)


def directed_triangle_distance(points: list[list[float]], other_points: list[list[float]],
                               triangles: list[list[int]]) -> float:
    if not points or not other_points or not triangles:
        return math.inf
    return max(min(point_triangle_distance(point, other_points[a], other_points[b], other_points[c])
                   for a, b, c in triangles) for point in points)


def compare_mesh(dimension: int, reference: dict[str, Any], candidate: dict[str, Any], delta: float) -> dict[str, Any]:
    structural = {
        "topologyMatchesDirect": reference["cells"] == candidate["cells"],
        "cellMaterialIdsMatchDirect": reference["materialIds"] == candidate["materialIds"],
        "levelSetMaterialMapMatchesDirect": reference["materialIdsByLevelSetIndex"] == candidate["materialIdsByLevelSetIndex"],
        "levelSetCountMatchesDirect": reference["levelSetCount"] == candidate["levelSetCount"],
    }
    if dimension == 2:
        forward = directed_mesh_distance(reference["points"], candidate["points"], candidate["cells"])
        backward = directed_mesh_distance(candidate["points"], reference["points"], reference["cells"])
        hausdorff = max(forward, backward)
        geometry = {"metric": "symmetric point-to-segment Hausdorff over mesh vertices",
                    "hausdorffUm": hausdorff, "errorInGridSteps": hausdorff / delta,
                    "withinOneTenthGrid": hausdorff <= 0.1 * delta}
    else:
        labels = sorted(set(reference["materialIds"]) | set(candidate["materialIds"]))
        per_material = {}
        for label in labels:
            lhs_cells = [cell for cell, value in zip(reference["cells"], reference["materialIds"]) if value == label]
            rhs_cells = [cell for cell, value in zip(candidate["cells"], candidate["materialIds"]) if value == label]
            lhs_points = sorted({index for cell in lhs_cells for index in cell})
            rhs_points = sorted({index for cell in rhs_cells for index in cell})
            lhs_vertices = [reference["points"][index] for index in lhs_points]
            rhs_vertices = [candidate["points"][index] for index in rhs_points]
            if lhs_cells and rhs_cells:
                forward = directed_triangle_distance(lhs_vertices, candidate["points"], rhs_cells)
                backward = directed_triangle_distance(rhs_vertices, reference["points"], lhs_cells)
                distance = max(forward, backward)
            else:
                distance = math.inf
            per_material[str(label)] = {"referenceTriangleCount": len(lhs_cells),
                                        "candidateTriangleCount": len(rhs_cells),
                                        "symmetricVertexToTriangleHausdorffUm": distance,
                                        "errorInGridSteps": distance / delta}
        max_distance = max((row["symmetricVertexToTriangleHausdorffUm"] for row in per_material.values()), default=math.inf)
        geometry = {"metric": "symmetric surface-vertex-to-triangle Hausdorff, computed separately per MaterialIds label",
                    "maxMaterialSurfaceHausdorffUm": max_distance,
                    "errorInGridSteps": max_distance / delta,
                    "withinOneTenthGrid": max_distance <= 0.1 * delta,
                    "materialSurfaceDistances": per_material}
    return {**structural, **geometry,
            "referencePointCount": len(reference["points"]), "candidatePointCount": len(candidate["points"]),
            "referenceCellCount": len(reference["cells"]), "candidateCellCount": len(candidate["cells"])}


def main(project_path: Path, cpu_path: Path, gpu_path: Path, cpu_guard: Path,
         gpu_guard: Path, output_path: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    gpu = json.loads(gpu_path.read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row.get("enabled"))
    cpu_step = cpu["steps"][step["id"]]
    delta = float(project["domain"]["gridDelta"])
    guards = {"cpu": guard_ok(cpu_guard), "gpu": guard_ok(gpu_guard)}
    gpu_initial = gpu.get("cases", [{}])[0].get("initial") if gpu.get("cases") else None
    cpu_initial = cpu.get("initial")
    initial_matches = bool(cpu_initial and gpu_initial and all(
        cpu_initial.get(key) == gpu_initial.get(key)
        for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex", "levelSetCount")))
    cases = []
    for case in gpu.get("cases", []):
        comparisons = compare_mesh(int(project["dimension"]), cpu_step["snapshot"], case["final"], delta)
        comparisons["callbacksObservedForEverySpecies"] = set(case.get("callbacksBySpecies", {})) == {
            row["label"] for row in step["species"]
        } and all(value > 0 for value in case.get("callbacksBySpecies", {}).values())
        cases.append({"sweeps": case["sweeps"], "nativeStages": len(case.get("stages", [])),
                      "elapsedSeconds": case.get("totalElapsedSeconds"), "callbacksBySpecies": case.get("callbacksBySpecies"),
                      "comparisons": comparisons})
    status = "exploratory-promising-not-accepted"
    report = {
        "status": status,
        "scope": "independent direct ViennaPS CPU MultiParticle reference versus ViennaPS 4.6.2 GPU_DISK symmetric single-species sub-processes",
        "engineVersions": cpu.get("installedVersions", cpu.get("versions")),
        "dimension": int(project["dimension"]), "operation": step["operation"],
        "geometryKind": project["geometry"]["kind"], "gridDelta": delta,
        "input": {"duration": step["duration"], "raysPerPoint": project["solver"]["raysPerPoint"],
                  "seed": project["solver"]["random"]["seed"], "species": step["species"],
                  "rateCoefficients": step["rateCoefficients"],
                  "depositMaterial": step.get("depositMaterial")},
        "cpuReference": {"engine": "CPU_DISK", "path": str(cpu_path), "guard": str(cpu_guard)},
        "gpuSplit": {"engine": gpu.get("engine"), "contextDeviceIndex": gpu.get("contextDeviceIndex"),
                     "path": str(gpu_path), "guard": str(gpu_guard),
                     "strategy": "symmetric operator splitting; stage seeds are recorded in the GPU result"},
        "guards": guards, "initialMatchesDirect": initial_matches, "cases": cases,
        "limitations": ["exploratory only; no product route or capability-matrix entry is implied",
                        "geometry tolerance is an audit metric, not an application acceptance threshold"],
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "dimension": report["dimension"],
                      "operation": report["operation"], "initialMatchesDirect": initial_matches,
                      "guards": guards, "cases": [{"sweeps": row["sweeps"], **row["comparisons"]}
                                                      for row in cases],
                      "output": str(output_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 7:
        raise SystemExit("usage: compare_multiparticle_gpu_split.py PROJECT CPU GPU CPU_GUARD GPU_GUARD OUTPUT")
    main(*(Path(value) for value in sys.argv[1:]))
