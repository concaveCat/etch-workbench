"""Independent direct ViennaPS reference for a GPU single-particle state continuation."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sys
import time
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage7_multiparticle_reference import direct_material, snapshot


def main(project_path: str, state_path: str, output_path: str, engine: str = "GPU_DISK") -> None:
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    state_file = Path(state_path).resolve()
    if not state_file.is_file():
        raise FileNotFoundError(state_file)

    dimension = int(project["dimension"])
    settings = project["domain"]
    boundary_name = settings.get("xBoundary", settings.get("boundaryXY"))
    boundary = (ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective"
                else ps.BoundaryType.PERIODIC_BOUNDARY)
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    if project.get("schemaVersion", 1) >= 3:
        ps.Length.setUnit("um")
        ps.Time.setUnit("s")
    if dimension == 2:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]),
                           float(settings["yExtent"]), boundary)
    ps.Reader(domain, str(state_file)).apply()
    initial = snapshot(domain, dimension)

    step = project["steps"][0]
    sign = -1.0 if step["operation"] == "etch" else 1.0
    geometry = project["geometry"]
    masks = []
    if geometry["kind"] == "stack":
        masks = list(dict.fromkeys(layer["material"] for layer in geometry["layers"]
                                   if layer["role"] == "mask"))
    model = ps.SingleParticleProcess(
        rate=sign * float(step["rateMagnitude"]),
        stickingProbability=float(step["stickingProbability"]),
        sourceExponent=float(step["sourceExponent"]),
        maskMaterials=[direct_material(name) for name in masks],
    )
    process = ps.Process(domain, model, float(step["duration"]))
    flux_engine = getattr(ps.FluxEngineType, engine)
    process.setFluxEngineType(flux_engine)

    requested_advection = project["solver"].get("advection") or {}
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    if requested_advection:
        advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested_advection["spatialScheme"])
        advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested_advection["temporalScheme"])
        for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                    "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
            setattr(advection, key, requested_advection[key])
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
    result = {
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "engine": engine,
        "elapsedSeconds": elapsed,
        "seed": int(rays.rngSeed),
        "threads": int(project["solver"]["cpuThreads"]),
        "sourceStateSha256": hashlib.sha256(state_file.read_bytes()).hexdigest(),
        "operation": step["operation"],
        "rate": sign * float(step["rateMagnitude"]),
        "duration": float(step["duration"]),
        "masks": masks,
        "initial": initial,
        "final": snapshot(domain, dimension),
    }
    Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        raise SystemExit("usage: gpu_singleparticle_state_continuation_reference.py PROJECT.json STATE.vpsd OUTPUT.json [CPU_DISK|GPU_DISK]")
    main(*sys.argv[1:])
