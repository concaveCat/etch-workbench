"""Independent direct ViennaPS references for preset × specialized GPU cases.

Each invocation is a bounded child. Model construction comes from the
independent Stage 8 native-reference fixture, never the product worker.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from gpu_geometry_direct_reference import effective_fin_construction, make_domain, snapshot
from stage8_direct_reference import make_model


def run(project: dict[str, Any], output: Path, scratch: Path, engine: str = "GPU_DISK",
        remove_stray_points: bool = False, gds_source: Path | None = None) -> None:
    scratch.mkdir(parents=True, exist_ok=True)
    dimension = int(project["dimension"])
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    geometry_readback: dict[str, Any] = {}
    if project.get("geometry", {}).get("kind") == "gds":
        domain, geometry_readback = make_domain(project, gds_source, scratch)
    else:
        domain, geometry_readback = make_domain(project)
    if remove_stray_points:
        domain.removeStrayPoints()
    domain.getGridDelta()
    domain.getBoundaryConditions()
    initial = snapshot(domain, dimension)
    if dimension == 3:
        domain.getBoundingBox()
    scratch.mkdir(parents=True, exist_ok=True)
    domain.saveSurfaceMesh(str(scratch / "initial.vtk"), addInterfaces=True,
                           sharpCorners=False, minNodeDistanceFactor=0.01)
    ps.Writer(domain, str(scratch / "state-initial.vpsd")).apply()

    step = project["steps"][0]
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(ps.MaterialRegistry.instance().getMaterial(step["depositMaterial"]))
    model, extras = make_model(step, domain, project)
    duration = float(step.get("duration", float(step["nativeParameters"].get("timeHours", 0.0)) * 3600.0))
    process = ps.Process(domain, model, duration)
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))

    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, project["solver"]["advection"]["spatialScheme"])
    advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, project["solver"]["advection"]["temporalScheme"])
    for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
        setattr(advection, key, project["solver"]["advection"][key])
    process.setParameters(advection)
    for parameter in extras:
        process.setParameters(parameter)

    rays = ps.RayTracingParameters()
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                "minNodeDistanceFactor", "smoothingNeighbors"):
        setattr(rays, key, project["solver"]["rayTracing"][key])
    rays.normalizationType = getattr(ps.NormalizationType, project["solver"]["rayTracing"]["normalizationType"])
    process.setParameters(rays)

    if dimension == 3:
        pre = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        nodes, triangles = pre.getNodes(), pre.getTriangles()
        pre.getLines(); pre.getTetras(); pre.getHexas(); pre.getVerticies()
        for data in (pre.getPointData(), pre.getCellData()):
            for index in range(data.getScalarDataSize()): data.getScalarData(index)
            for index in range(data.getVectorDataSize()): data.getVectorData(index)
        if not nodes or not triangles:
            raise RuntimeError("direct specialized pre-process surface is empty")
        del pre

    previous = Path.cwd()
    try:
        os.chdir(scratch)
        process.setIntermediateOutputPath(str(scratch))
        process.apply()
    finally:
        os.chdir(previous)
    result = {
        "status": "succeeded", "model": step["nativeModel"], "operation": step["operation"],
        "dimension": dimension, "actualEngine": engine,
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "seed": int(rays.rngSeed), "initial": initial, "final": snapshot(domain, dimension),
        "geometryReadback": geometry_readback,
    }
    if project.get("geometry", {}).get("kind") == "fin":
        result["finConstruction"] = effective_fin_construction(project)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: gpu_specialized_geometry_direct_reference.py PROJECT.json OUTPUT.json SCRATCH_DIR [ENGINE] [remove-stray-points] [--gds-source GDS_PATH]")
    project_path, output_path, scratch_path = map(Path, sys.argv[1:4])
    extras = sys.argv[4:]
    engine = next((item for item in extras if item in {"GPU_DISK", "CPU_DISK"}), "GPU_DISK")
    remove_stray_points = "remove-stray-points" in extras
    gds_source = None
    if "--gds-source" in extras:
        index = extras.index("--gds-source")
        if index + 1 >= len(extras):
            raise SystemExit("--gds-source requires a path")
        gds_source = Path(extras[index + 1]).resolve()
    run(json.loads(project_path.read_text(encoding="utf-8")), output_path, scratch_path,
        engine, remove_stray_points, gds_source)
