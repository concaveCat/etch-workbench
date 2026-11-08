"""Independent direct ViennaPS GPU_DISK reference for schema-7 custom materials."""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from stage7_multiparticle_reference import direct_material, make_domain, register_custom_materials, snapshot


def main(project_path: str, output_path: str) -> None:
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    register_custom_materials(project.get("materialDefinitions", []))
    domain = make_domain(project)
    initial = snapshot(domain, project["dimension"])
    step = project["steps"][0]
    rate = -float(step["rateMagnitude"]) if step["operation"] == "etch" else float(step["rateMagnitude"])
    model = ps.SingleParticleProcess(rate=rate,
        stickingProbability=float(step["stickingProbability"]), sourceExponent=float(step["sourceExponent"]),
        maskMaterials=[])
    sign = -1.0 if step["operation"] == "etch" else 1.0
    for name, magnitude in step["materialRates"].items():
        model.setMaterialRate(direct_material(name), sign * float(magnitude))
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    process.setParameters(rays)
    process.apply()
    result = {"versions": {"viennaps": importlib.metadata.version("viennaps"),
                           "viennals": importlib.metadata.version("viennals")},
              "engine": "GPU_DISK", "seed": int(rays.rngSeed), "initial": initial,
              "final": snapshot(domain, project["dimension"])}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3: raise SystemExit("usage: stage9_custom_material_gpu_reference.py PROJECT.json OUTPUT.json")
    main(*sys.argv[1:])
