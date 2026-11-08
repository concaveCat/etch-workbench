"""Compare a bounded production worker result with an independent CPU Domain."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_multiparticle_gpu_split import compare_mesh, guard_ok


def numeric_block(lines: list[str], start: int, count: int) -> tuple[list[float], int]:
    values: list[float] = []
    index = start
    while index < len(lines) and len(values) < count:
        try:
            values.extend(float(value) for value in lines[index].split())
        except ValueError:
            break
        index += 1
    if len(values) != count:
        raise ValueError(f"legacy VTK block at line {start + 1} contains {len(values)} values, expected {count}")
    return values, index


def parse_vtk(path: Path, material_map: list[int], level_set_count: int) -> dict[str, Any]:
    lines = path.read_text(encoding="ascii").splitlines()
    point_index = next(index for index, line in enumerate(lines) if line.startswith("POINTS "))
    _, point_count_text, _ = lines[point_index].split()
    point_count = int(point_count_text)
    values, point_end = numeric_block(lines, point_index + 1, point_count * 3)
    points = [values[index:index + 3] for index in range(0, len(values), 3)]

    cell_index = next(index for index, line in enumerate(lines) if line.startswith("CELLS "))
    _, cell_count_text, _ = lines[cell_index].split()
    cell_count = int(cell_count_text)
    cells = []
    for line in lines[cell_index + 1:cell_index + 1 + cell_count]:
        values = [int(value) for value in line.split()]
        if not values or values[0] != len(values) - 1:
            raise ValueError(f"invalid legacy VTK cell in {path.name}")
        cells.append(values[1:])
    cell_data_index = next(index for index, line in enumerate(lines) if line.startswith("CELL_DATA "))
    material_index = next(index for index, line in enumerate(lines) if line.startswith("SCALARS MaterialIds "))
    lookup_index = material_index + 1
    if not lines[lookup_index].startswith("LOOKUP_TABLE "):
        raise ValueError(f"MaterialIds scalar table is malformed in {path.name}")
    material_values, _ = numeric_block(lines, lookup_index + 1, cell_count)
    cell_data_count = int(lines[cell_data_index].split()[1])
    if cell_data_count != cell_count:
        raise ValueError(f"CELL_DATA size differs from CELLS in {path.name}")
    return {"points": points, "cells": cells, "materialIds": [int(value) for value in material_values],
            "materialIdsByLevelSetIndex": material_map, "levelSetCount": level_set_count}


def initial_comparison(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    max_coordinate_delta = max((abs(lhs - rhs) for a, b in zip(reference["points"], candidate["points"])
                                for lhs, rhs in zip(a, b)), default=0.0)
    return {"pointCountMatches": len(reference["points"]) == len(candidate["points"]),
            "cellCountMatches": len(reference["cells"]) == len(candidate["cells"]),
            "topologyMatches": reference["cells"] == candidate["cells"],
            "materialIdsMatch": reference["materialIds"] == candidate["materialIds"],
            "materialMapMatches": reference["materialIdsByLevelSetIndex"] == candidate["materialIdsByLevelSetIndex"],
            "levelSetCountMatches": reference["levelSetCount"] == candidate["levelSetCount"],
            "maxCoordinateDeltaUm": max_coordinate_delta,
            "coordinatesWithin1e-6Um": max_coordinate_delta <= 1e-6}


def main(project_path: Path, cpu_path: Path, worker_dir: Path, cpu_guard: Path,
         worker_guard: Path, output_path: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    worker = json.loads((worker_dir / "worker-result.json").read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row.get("enabled"))
    step_metadata = next(row for row in worker["snapshots"] if row.get("stepId") == step["id"])
    initial_metadata = next(row for row in worker["snapshots"] if row.get("stepId") == "initial")
    initial_product = parse_vtk(worker_dir / "initial.vtk", initial_metadata["materialIdsByLevelSetIndex"],
                                len(initial_metadata["materialIdsByLevelSetIndex"]))
    final_product = parse_vtk(worker_dir / f"{step['id']}.vtk", step_metadata["materialIdsByLevelSetIndex"],
                              len(step_metadata["materialIdsByLevelSetIndex"]))
    cpu_step = cpu["steps"][step["id"]]
    initial = initial_comparison(cpu["initial"], initial_product)
    final = compare_mesh(int(project["dimension"]), cpu_step["snapshot"], final_product,
                         float(project["domain"]["gridDelta"]))
    record = next(row for row in worker["stepRecords"] if row.get("stepId") == step["id"])
    actual = record["actualParameters"]
    execution = actual["multiParticleExecution"]
    device = worker["selectedGpuDevice"]
    device_readback = actual["cudaDeviceReadback"]
    state = step_metadata["nativeState"]
    state_path = worker_dir / state["fileName"]
    stage_rows = execution["stages"]
    expected_stage_count = (2 * len(step["species"]) - 1) * execution["sweeps"]
    callbacks = execution["callbacksBySpecies"]
    checks = {
        "versions": worker["versions"] == {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "workerGuardClean": guard_ok(worker_guard), "cpuReferenceGuardClean": guard_ok(cpu_guard),
        "engineGPU_DISK": record["actualEngine"] == "GPU_DISK",
        "allStageEnginesGPU_DISK": len(stage_rows) == expected_stage_count and all(row["engine"] == "GPU_DISK" for row in stage_rows),
        "selectedUuidMatchesHostRTX3060": device["uuid"] == "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
        "everyStageRecordsSameUuid": all(row["gpuDeviceUuid"] == device["uuid"] for row in stage_rows),
        "deviceReadbackSynchronized": (device_readback["readbackStatus"] == "verified"
                                       and device_readback["afterProcessApply"]["readbackStatus"] == "verified"
                                       and device_readback["uuid"] == device["uuid"]
                                       and device_readback["afterProcessApply"]["uuid"] == device["uuid"]),
        "allSpeciesRateCallbacksObserved": set(callbacks) == {row["label"] for row in step["species"]}
                                           and all(value > 0 for value in callbacks.values()),
        "initialDirectGeometryMatches": all(initial[key] for key in (
            "pointCountMatches", "cellCountMatches", "topologyMatches", "materialIdsMatch",
            "materialMapMatches", "levelSetCountMatches", "coordinatesWithin1e-6Um")),
        "nativeStateSavedAndHashMatches": state_path.is_file() and hashlib.sha256(state_path.read_bytes()).hexdigest() == state["stateId"],
    }
    if project["dimension"] == 2:
        checks["finalTopologyAndMaterialsMatch"] = (final["topologyMatchesDirect"]
                                                     and final["cellMaterialIdsMatchDirect"]
                                                     and final["levelSetMaterialMapMatchesDirect"])
        checks["finalGeometryWithinOneTenthGrid"] = final["withinOneTenthGrid"]
    else:
        checks["finalMaterialMapsMatch"] = (final["levelSetMaterialMapMatchesDirect"]
                                             and set(final["materialSurfaceDistances"]) ==
                                             {str(value) for value in set(cpu_step["snapshot"]["materialIds"])
                                              | set(final_product["materialIds"])})
        checks["finalPerMaterialGeometryWithinOneTenthGrid"] = final["withinOneTenthGrid"]
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "scope": "bounded production worker MultiParticle GPU split versus independent direct CPU_DISK ViennaPS reference",
        "input": {"project": str(project_path), "dimension": project["dimension"],
                  "operation": step["operation"], "geometryKind": project["geometry"]["kind"],
                  "species": step["species"], "rateCoefficients": step["rateCoefficients"],
                  "duration": step["duration"], "seed": worker["actualSeed"],
                  "raysPerPoint": project["solver"]["raysPerPoint"]},
        "device": device, "strategy": execution,
        "guards": {"worker": str(worker_guard), "cpuReference": str(cpu_guard)},
        "initialComparison": initial, "finalComparison": final, "checks": checks,
        "nativeState": state,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": checks,
                      "initial": initial, "final": final,
                      "output": str(output_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 7:
        raise SystemExit("usage: compare_multiparticle_product_worker.py PROJECT CPU WORKER_DIR CPU_GUARD WORKER_GUARD OUTPUT")
    main(*(Path(value) for value in sys.argv[1:]))
