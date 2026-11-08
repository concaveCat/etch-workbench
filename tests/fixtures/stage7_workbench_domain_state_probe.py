"""Recreate the exact WebUI 3D geometry and state-save sequence for comparison."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "worker"))

import mesh3d
from worker import simulate as worker


def signature(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=.01)
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells = [[int(value) for value in row] for row in cells]
    payload = json.dumps({"points": points, "cells": cells, "materialIds": material_ids},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    mapping = domain.getMaterialMap()
    return {"pointCount": len(points), "cellCount": len(cells),
            "materialCounts": dict(Counter(material_ids)),
            "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
            "levelSetCount": int(domain.getNumberOfLevelSets()), "surfaceSha256": hashlib.sha256(payload).hexdigest()}


project_path, output_path, _engine = sys.argv[1:]
project = json.loads(Path(project_path).read_text(encoding="utf-8"))
gpu_readback = None
if _engine.startswith("GPU_"):
    inventory = worker.cuda_devices(refresh=True)
    selected = worker.resolve_gpu_device(os.environ["CUDA_VISIBLE_DEVICES"], inventory)
    gpu_readback = worker.cuda_runtime_readback(selected)
import viennaps as ps
ps.Length.setUnit("um"); ps.Time.setUnit("s")
ps.setDimension(int(project["dimension"]))
ps.setNumThreads(int(project["solver"]["cpuThreads"]))
worker.configure_native_materials(ps, project.get("materialDefinitions", []))
mesh3d.configure_material_catalog(worker.MATERIAL_ROWS)
domain = mesh3d.make_domain(ps, project)
worker.make_v3_geometry(ps, domain, project, config={}, run_dir=Path(output_path).parent)
states = {"afterWorkerGeometryBuilder": signature(domain, project["dimension"])}
states["beforeWorkerSaveSnapshot"] = signature(domain, project["dimension"])
Path(output_path).write_text(json.dumps({"viennapsModulePath": ps.__file__, "dimension": project["dimension"],
                                         "diagnostic": states, "materialCatalog": worker.MATERIAL_NAMES},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
run_dir = Path(output_path).with_suffix(".assets").resolve()
run_dir.mkdir(parents=True, exist_ok=True)
worker.save_snapshot(ps, domain, run_dir, run_id="domain-state-probe", step_id="initial",
                     step_name="初始结构", step_index=0, requested=project)
states["afterWorkerSaveSnapshot"] = signature(domain, project["dimension"])
domain.saveSurfaceMesh(str(run_dir / "surface-second-pass.vtk"), addInterfaces=True,
                       sharpCorners=False, minNodeDistanceFactor=0.01)
states["afterSecondNativeSurfaceSave"] = signature(domain, project["dimension"])
domain.saveSurfaceMesh(str(run_dir / "surface-third-pass.vtk"), addInterfaces=True,
                       sharpCorners=False, minNodeDistanceFactor=0.01)
states["afterThirdNativeSurfaceSave"] = signature(domain, project["dimension"])
state_ref = worker.save_native_state(ps, domain, run_dir, project=project, run_id="domain-state-probe",
                                     step_id="initial", viennaps_version=importlib.metadata.version("viennaps"),
                                     viennals_version=importlib.metadata.version("viennals"))
states["afterWorkerSaveNativeState"] = signature(domain, project["dimension"])
state_path = run_dir / state_ref["fileName"]
settings = project["domain"]
boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if settings.get("boundaryXY") == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
reloaded = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), float(settings["yExtent"]), boundary)
ps.Reader(reloaded, str(state_path)).apply()
states["afterReader"] = signature(reloaded, project["dimension"])
step = next(row for row in project["steps"] if row.get("enabled"))
reloaded.duplicateTopLevelSet(worker.material_handle(ps, step["depositMaterial"]))
states["afterDepositDuplicate"] = signature(reloaded, project["dimension"])
result = {"viennapsModulePath": ps.__file__,
          "versions": {"viennaps": importlib.metadata.version("viennaps"),
                       "viennals": importlib.metadata.version("viennals")},
          "dimension": project["dimension"], "diagnostic": states, "cudaReadback": gpu_readback,
          "nativeState": state_ref, "statePath": str(state_path), "steps": {}}
Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
print(json.dumps({"versions": result["versions"], "dimension": result["dimension"], "states": states}), flush=True)
