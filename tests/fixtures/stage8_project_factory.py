"""Small valid schema-7 inputs shared by Stage 8 acceptance drivers."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.specialized_models import specialized_catalog


def make_project(model_id: str, dimension: int = 2) -> dict[str, Any]:
    catalog = specialized_catalog()
    spec = next(row for row in catalog["models"] if row["id"] == model_id)
    geometry = {
        "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
        "layers": [
            {"id": "oxide", "name": "Oxide", "material": "SiO2", "thickness": 0.4, "role": "film"},
            {"id": "nitride", "name": "Nitride", "material": "Si3N4", "thickness": 0.4, "role": "mask"},
        ],
        "pattern": {"kind": "none"},
    }
    geometry_names = {"Si", "SiO2", "Si3N4"}
    native_parameters: dict[str, Any] = {}
    for field in spec["fields"]:
        if field["kind"] == "materialRates":
            required = {"Si", "SiGe"} if model_id == "selectiveEpitaxy" else geometry_names if model_id == "wetEtch" else set()
            native_parameters[field["key"]] = {name: deepcopy(field["default"]) for name in required}
        elif field["kind"] == "fluorocarbonMaterials":
            native_parameters[field["key"]] = {name: deepcopy(field["defaults"]) for name in sorted(geometry_names | {"Mask", "Polymer"})}
        else:
            native_parameters[field["key"]] = deepcopy(field["default"])
    step = {"id": f"stage8_{model_id}", "name": spec["label"], "enabled": True, "model": "specialized",
            "nativeModel": model_id, "operation": spec["operation"], "nativeParameters": native_parameters}
    if model_id != "oxidation":
        step["duration"] = 0.02
    if spec.get("depositMaterial"):
        step["depositMaterial"] = spec["depositMaterial"]
    elif spec.get("depositMaterialSelectable"):
        step["depositMaterial"] = "Al2O3"
    project: dict[str, Any] = {
        "schemaVersion": 7, "name": f"Stage 8 {model_id} {dimension}D", "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0,
                   **({"xBoundary": "reflective"} if dimension == 2 else {"yExtent": 4.0, "boundaryXY": "reflective"})},
        "geometry": geometry, "steps": [step],
        "solver": {"timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
                   "random": {"mode": "fixed", "seed": 271828}, "enginePreference": "cpu", "timeoutSeconds": 90,
                   "advection": {"spatialScheme": "ENGQUIST_OSHER_1ST_ORDER", "temporalScheme": "FORWARD_EULER",
                                 "adaptiveTimeStepping": False, "adaptiveTimeStepSubdivisions": 20,
                                 "calculateIntermediateVelocities": False, "checkDissipation": True,
                                 "dissipationAlpha": 1.0, "ignoreVoids": False, "velocityOutput": False},
                   "rayTracing": {"diskRadius": 0.0, "ignoreFluxBoundaries": False, "maxBoundaryHits": 80,
                                  "maxReflections": 3, "minNodeDistanceFactor": 0.05,
                                  "normalizationType": "SOURCE", "smoothingNeighbors": 1}},
        "outputSettings": {"levelSets": False, "levelSetMeshes": False, "volumeMesh": False, "nativeMetadata": False},
    }
    if dimension == 2:
        project["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        project["view3D"] = {"projection": "parallel", "camera": None, "displayMode": "surface",
                             "visibleMaterialIds": [0, 10, 30], "opacityByMaterial": {"0": 1.0, "10": 1.0, "30": 1.0},
                             "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
                             "section": {"enabled": False, "axis": "y", "position": 0.0}}
    return project
