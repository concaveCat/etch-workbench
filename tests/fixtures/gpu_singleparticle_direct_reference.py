"""Independent ViennaPS GPU_DISK reference for schema-7 singleParticle inputs."""
from __future__ import annotations

import importlib.metadata
import json
import math
import sys
import time
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from stage7_multiparticle_reference import direct_material, make_domain, snapshot


def main(project_path: str, output_path: str, engine: str = "GPU_DISK") -> None:
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    domain = make_domain(project)
    initial = snapshot(domain, project["dimension"])
    step = project["steps"][0]
    sign = -1.0 if step["operation"] == "etch" else 1.0
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(direct_material(step["depositMaterial"]))
    geometry = project["geometry"]
    masks = []
    if geometry["kind"] == "stack":
        masks = list(dict.fromkeys(layer["material"] for layer in geometry["layers"]
                                   if layer["role"] == "mask"))
    model = ps.SingleParticleProcess(rate=sign * float(step["rateMagnitude"]),
        stickingProbability=float(step["stickingProbability"]),
        sourceExponent=float(step["sourceExponent"]),
        maskMaterials=[direct_material(name) for name in masks])
    process = ps.Process(domain, model, float(step["duration"]))
    if engine not in {"CPU_DISK", "GPU_DISK"}:
        raise ValueError("direct single-particle benchmark supports CPU_DISK or GPU_DISK")
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))

    advection = ps.AdvectionParameters()
    requested_advection = project["solver"].get("advection")
    if requested_advection:
        advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
        advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested_advection["spatialScheme"])
        advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested_advection["temporalScheme"])
        for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                    "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
            setattr(advection, key, requested_advection[key])
    else:
        advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)

    rays = ps.RayTracingParameters()
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    for key, value in project["solver"].get("rayTracing", {}).items():
        if key == "normalizationType":
            rays.normalizationType = getattr(ps.NormalizationType, value)
        else:
            setattr(rays, key, value)
    process.setParameters(rays)
    started = time.perf_counter()
    process.apply()
    elapsed = time.perf_counter() - started
    final = snapshot(domain, project["dimension"])
    result = {"versions": {"viennaps": importlib.metadata.version("viennaps"),
                           "viennals": importlib.metadata.version("viennals")},
              "engine": engine, "elapsedSeconds": elapsed, "seed": int(rays.rngSeed), "threads": int(project["solver"]["cpuThreads"]),
              "operation": step["operation"], "rate": sign * float(step["rateMagnitude"]),
              "duration": float(step["duration"]), "masks": masks,
              "initial": initial, "final": final}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                 encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: gpu_singleparticle_direct_reference.py PROJECT.json OUTPUT.json [CPU_DISK|GPU_DISK]")
    main(*sys.argv[1:])
