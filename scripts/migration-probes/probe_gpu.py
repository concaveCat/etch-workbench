"""Bounded probe: stock ViennaPS 4.7.0 GPU disk engine (RTX 3060)."""
import hashlib, json, sys
import viennaps as ps

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
(ROOT / "var" / "probes").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "scripts" / "migration-probes"))
from common import write

ps.setDimension(2)
ps.Logger.setLogLevel(ps.LogLevel.ERROR)
ps.setNumThreads(1)

def run(engine):
    d = ps.Domain(0.2, 4.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(d, 0.0, ps.Material.Si).apply()
    model = ps.SingleParticleProcess(rate=1.0, stickingProbability=0.5, sourceExponent=1.0,
                                     maskMaterial=ps.Material.Mask)
    p = ps.Process(d, model, 0.5)
    p.setFluxEngineType(engine)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = 64
    rays.rngSeed = 314159
    rays.useRandomSeeds = False
    p.setParameters(rays)
    p.apply()
    nodes = [tuple(round(float(x), 6) for x in n) for n in d.getSurfaceMesh().getNodes()]
    h = hashlib.sha256(repr(sorted(nodes)).encode()).hexdigest()
    return {"hash": h, "points": len(nodes), "zmin": min(n[1] for n in nodes),
            "zmax": max(n[1] for n in nodes), "zs": [n[1] for n in nodes]}

cpu = run(ps.FluxEngineType.CPU_DISK)
gpu = run(ps.FluxEngineType.GPU_DISK)
out = {"viennaps": ps.__version__, "viennals": ps.ls.version,
       "gpuAvailable": bool(ps.gpuAvailable()), "cpu": cpu, "gpu": gpu}
dev = max(abs(a - b) for a, b in zip(cpu["zs"], gpu["zs"]))
out["maxAbsCpuGpuDeviation"] = dev
out["checks"] = {"gpu_available": out["gpuAvailable"], "gpu_ran": gpu["points"] > 0,
                 "gpu_executes": gpu["points"] > 0}
out["status"] = "ok" if all(out["checks"].values()) else "check-failed"
write(str(ROOT / "var" / "probes") + "/gpu.json", out)
print(json.dumps(out))
