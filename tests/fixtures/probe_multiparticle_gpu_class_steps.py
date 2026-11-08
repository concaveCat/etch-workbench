"""Locate a crash in the installed GPU-specific MultiParticle class route."""
from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/fixtures"))
from gpu_geometry_direct_reference import make_domain


def values(data: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index in range(data.getScalarDataSize()):
        label = str(data.getScalarDataLabel(index))
        result[f"scalar:{label}"] = [float(value) for value in data.getScalarData(index)]
    for index in range(data.getVectorDataSize()):
        label = str(data.getVectorDataLabel(index))
        result[f"vector:{label}"] = [[float(value) for value in row] for row in data.getVectorData(index)]
    return result


def mark(stage: str, **extra: Any) -> None:
    print(json.dumps({"stage": stage, **extra}, ensure_ascii=False), flush=True)


def main(project_path: Path, output_path: Path, initialize_context: bool = False) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    dimension = int(project["dimension"])
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    mark("viennaps-imported", dimension=dimension,
         versions={"viennaps": importlib.metadata.version("viennaps"),
                   "viennals": importlib.metadata.version("viennals")})
    if initialize_context:
        context = ps.gpu.Context.createContext()
        mark("gpu-context-created", deviceID=int(context.deviceID), modulePath=context.getModulePath())
    domain, readback = make_domain(project)
    mark("domain-created", readback=readback)
    dimension_module = ps.d2 if dimension == 2 else ps.d3
    model = dimension_module.gpu.MultiParticleProcess()
    mark("gpu-specific-model-created", modelClass=f"{type(model).__module__}.{type(model).__qualname__}")
    labels = []
    for species in project["steps"][0]["species"]:
        label = str(species["label"])
        labels.append(label)
        if species["kind"] == "ion":
            model.addIonParticle(
                sourcePower=float(species["sourcePower"]), thetaRMin=float(species["thetaRMin"]),
                thetaRMax=float(species["thetaRMax"]), minAngle=float(species["minAngle"]),
                B_sp=float(species["B_sp"]), meanEnergy=float(species["meanEnergy"]),
                sigmaEnergy=float(species["sigmaEnergy"]), thresholdEnergy=float(species["thresholdEnergy"]),
                inflectAngle=float(species["inflectAngle"]), n=int(species["n"]), label=label)
        else:
            model.addNeutralParticle(float(species["defaultStickingProbability"]), label=label)
        mark("species-added", label=label, kind=species["kind"])
    operation_sign = -1 if project["steps"][0]["operation"] == "etch" else 1
    model.setRateFunction(lambda fluxes, _material:
                          float(operation_sign) * 0.025 * sum(float(value) for value in fluxes))
    mark("rate-function-added", speciesCount=len(labels))
    process = ps.Process(domain, model, float(project["steps"][0]["duration"]))
    mark("process-created")
    process.setFluxEngineType(ps.FluxEngineType.GPU_DISK)
    mark("gpu-engine-set")
    mesh = process.calculateFlux()
    mark("flux-calculated")
    result = {"status": "succeeded", "dimension": dimension, "engine": "GPU_DISK",
              "modelClass": f"{type(model).__module__}.{type(model).__qualname__}",
              "speciesOrder": labels, "points": [[float(value) for value in row] for row in mesh.getNodes()],
              "cellData": values(mesh.getCellData()), "pointData": values(mesh.getPointData()),
              "geometryReadback": readback}
    output_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8")
    mark("result-written", fields=sorted(result["cellData"]))


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: probe_multiparticle_gpu_class_steps.py PROJECT.json OUTPUT.json [initialize-context]")
    main(Path(sys.argv[1]), Path(sys.argv[2]), len(sys.argv) == 4 and sys.argv[3] == "initialize-context")
