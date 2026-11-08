"""Inspect native single-pass GPU/CPU flux meshes for a configured MultiParticle input."""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
from gpu_geometry_direct_reference import make_domain as make_preset_domain
from stage7_multiparticle_reference import add_species, make_domain as make_stage7_domain, register_custom_materials


def values(data: Any, prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for i in range(data.getScalarDataSize()):
        label = str(data.getScalarDataLabel(i))
        result[f"{prefix}scalar:{label}"] = [float(v) for v in data.getScalarData(i)]
    for i in range(data.getVectorDataSize()):
        label = str(data.getVectorDataLabel(i))
        result[f"{prefix}vector:{label}"] = [[float(v) for v in row] for row in data.getVectorData(i)]
    return result


def run(project_path: Path, output_path: Path, engine: str, model_route: str = "generic",
        vtp_output_path: Path | None = None) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    dimension = int(project["dimension"])
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    if project.get("geometry", {}).get("kind") in {"plane", "stack"}:
        register_custom_materials(project.get("materialDefinitions", []))
        domain = make_stage7_domain(project)
        readback = {"source": "independent Stage 7 ViennaPS geometry fixture",
                    "levelSetCount": int(domain.getNumberOfLevelSets()),
                    "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i))
                                                   for i in range(domain.getMaterialMap().size())]}
    else:
        domain, readback = make_preset_domain(project)
    step = project["steps"][0]
    dimension_module = ps.d2 if dimension == 2 else ps.d3
    if model_route == "gpu-specific":
        model_class = dimension_module.gpu.MultiParticleProcess
    elif model_route == "generic":
        model_class = dimension_module.MultiParticleProcess
    else:
        raise ValueError(f"unknown MultiParticle binding route: {model_route}")
    model = model_class()
    labels = add_species(model, step)
    sign = -1.0 if step["operation"] == "etch" else 1.0
    model.setRateFunction(lambda fluxes, material: sign * 0.025 * sum(float(value) for value in fluxes))
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    rays = ps.RayTracingParameters()
    solver = project["solver"]
    rays.raysPerPoint = int(solver["raysPerPoint"])
    rays.rngSeed = int(solver["random"]["seed"])
    rays.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                "minNodeDistanceFactor", "smoothingNeighbors"):
        setattr(rays, key, solver["rayTracing"][key])
    rays.normalizationType = getattr(ps.NormalizationType, solver["rayTracing"]["normalizationType"])
    process.setParameters(rays)
    mesh = process.calculateFlux()
    point_data = values(mesh.getPointData(), "point:")
    cell_data = values(mesh.getCellData(), "cell:")
    topology = {
        "lines": [[int(v) for v in row] for row in mesh.getLines()],
        "triangles": [[int(v) for v in row] for row in mesh.getTriangles()],
        "tetras": [[int(v) for v in row] for row in mesh.getTetras()],
        "hexas": [[int(v) for v in row] for row in mesh.getHexas()],
        "vertices": [[int(v) for v in row] for row in mesh.getVerticies()],
    }
    topology = {name: cells for name, cells in topology.items() if cells}
    points = [[float(v) for v in row] for row in mesh.getNodes()]
    mesh_identity = hashlib.sha256(json.dumps({"points": points, "topology": topology}, sort_keys=True,
                                              separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if vtp_output_path is not None:
        vtp_output_path.parent.mkdir(parents=True, exist_ok=True)
        ps.ls.VTKWriter(mesh, ps.ls.FileFormatEnum.VTP, str(vtp_output_path)).apply()
        if not vtp_output_path.is_file() or vtp_output_path.stat().st_size <= 0:
            raise RuntimeError("ViennaLS VTKWriter did not create the requested native flux VTP")
    result = {
        "status": "succeeded", "engine": engine, "dimension": dimension,
        "modelRoute": model_route, "nativeModelClass": f"{type(model).__module__}.{type(model).__qualname__}",
        "speciesOrder": labels, "speciesCount": len(labels),
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "points": points,
        "cells": [[int(v) for v in row] for row in (mesh.getLines() if dimension == 2 else mesh.getTriangles())],
        "topology": topology,
        "meshIdentity": mesh_identity,
        "pointData": point_data, "cellData": cell_data,
        "geometryReadback": readback,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"engine": engine, "dimension": dimension,
                      "points": len(result["points"]), "cells": len(result["cells"]),
                      "pointFields": list(point_data), "cellFields": list(cell_data),
                      "speciesCount": len(labels)}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5, 6):
        raise SystemExit("usage: probe_multiparticle_single_pass_flux.py PROJECT.json OUTPUT.json CPU_DISK|GPU_DISK [generic|gpu-specific] [OUTPUT.vtp]")
    run(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3],
        sys.argv[4] if len(sys.argv) >= 5 else "generic",
        Path(sys.argv[5]) if len(sys.argv) == 6 else None)
