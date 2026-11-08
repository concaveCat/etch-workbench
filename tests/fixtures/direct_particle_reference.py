"""Direct ViennaPS reference call; invoke only through bounded_child.py."""

from __future__ import annotations

import json
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import viennaps as ps

from worker.simulate import mesh_report


GRID_DELTA = 0.2
X_EXTENT = 4.0
THREADS = 1
RATE = -0.2
DURATION = 0.2
TIME_STEP_RATIO = 0.25
RAYS_PER_POINT = 64
SEED = 2771095476
STICKING_PROBABILITY = 0.5
SOURCE_EXPONENT = 1.0


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    ps.setDimension(2)
    ps.setNumThreads(THREADS)
    domain = ps.Domain(GRID_DELTA, X_EXTENT, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()

    model = ps.SingleParticleProcess(
        rate=RATE,
        stickingProbability=STICKING_PROBABILITY,
        sourceExponent=SOURCE_EXPONENT,
        maskMaterial=ps.Material.Mask,
    )
    process = ps.Process(domain, model, DURATION)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = TIME_STEP_RATIO
    process.setParameters(advection)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = RAYS_PER_POINT
    rays.rngSeed = SEED
    rays.useRandomSeeds = False
    process.setParameters(rays)
    process.apply()

    mesh = mesh_report(domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01))
    result = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "gridDeltaReadback": float(domain.getGridDelta()),
        "boundaryValuesReadback": [str(value) for value in domain.getBoundaryConditions()],
        "threads": THREADS,
        "engine": "CPU_DISK",
        "parameters": {
            "rate": RATE,
            "duration": DURATION,
            "timeStepRatio": float(advection.timeStepRatio),
            "raysPerPoint": int(rays.raysPerPoint),
            "rngSeed": int(rays.rngSeed),
            "useRandomSeeds": bool(rays.useRandomSeeds),
            "stickingProbability": STICKING_PROBABILITY,
            "sourceExponent": SOURCE_EXPONENT,
        },
        "points": mesh["points"],
        "lines": mesh["lines"],
        "materialIds": [int(value) for value in mesh["cellData"]["MaterialIds"]],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("versions", "gridDeltaReadback", "boundaryValuesReadback", "threads", "engine", "parameters")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
