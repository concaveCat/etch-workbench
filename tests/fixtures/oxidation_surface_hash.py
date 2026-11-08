"""Run one schema-7 oxidation project through the native worker path.

The installed ViennaPS proxy modules keep dimension state process-wide:
creating a 2D Domain and then switching to 3D in the same interpreter can
return a stale single-interface 3D mesh. Tests that need a trustworthy 3D run
therefore execute this fixture in a fresh subprocess and compare the returned
surface hash. It prints one compact JSON object on stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import viennaps as ps  # noqa: E402

from backend.validation import validate_project  # noqa: E402
from worker.specialized import build_specialized_model  # noqa: E402


def project(dimension: int, *, delta: float, extent: float, oxide: float,
            orientation: str, time_hours: float, time_step_hours: float,
            advanced: dict | None) -> dict:
    if dimension == 2:
        domain = {"gridDelta": delta, "xExtent": extent,
                  "xBoundary": "reflective"}
    else:
        domain = {"gridDelta": delta, "xExtent": extent, "yExtent": extent,
                  "boundaryXY": "reflective"}
    step = {
        "id": "stage8_oxidation", "name": "Oxidation probe", "enabled": True,
        "operation": "native", "model": "specialized", "nativeModel": "oxidation",
        "nativeParameters": {
            "oxidant": "Dry", "orientation": orientation, "temperatureC": 1000,
            "pressureAtm": 1, "timeHours": time_hours,
            "timeStepHours": time_step_hours, "transferCoefficientUmHr": 0.1,
            "initialOxideThicknessUm": 0, "reactionActivationVolumeM3": 0,
            "diffusionActivationVolumeM3": 0, "maskMaterial": "",
            "siliconMaterial": "Si", "oxideMaterial": "SiO2",
        },
    }
    if advanced is not None:
        step["advancedOxidation"] = advanced
    payload = {
        "schemaVersion": 7, "name": "Oxidation surface probe",
        "dimension": dimension,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain,
        "geometry": {
            "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
            "layers": [{"id": "oxide", "name": "Oxide",
                        "material": "SiO2", "thickness": oxide,
                        "role": "film"}],
            "pattern": {"kind": "none"},
        },
        "steps": [step],
        "solver": {
            "timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 271828},
            "enginePreference": "cpu", "timeoutSeconds": 180,
            "advection": {"spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
                          "temporalScheme": "FORWARD_EULER",
                          "adaptiveTimeStepping": False,
                          "adaptiveTimeStepSubdivisions": 20,
                          "calculateIntermediateVelocities": False,
                          "checkDissipation": True, "dissipationAlpha": 1.0,
                          "ignoreVoids": False, "velocityOutput": False},
            "rayTracing": {"diskRadius": 0.0, "ignoreFluxBoundaries": False,
                           "maxBoundaryHits": 80, "maxReflections": 3,
                           "minNodeDistanceFactor": 0.05,
                           "normalizationType": "SOURCE",
                           "smoothingNeighbors": 1},
        },
        "outputSettings": {"levelSets": False, "levelSetMeshes": False,
                           "volumeMesh": False, "nativeMetadata": False},
    }
    if dimension == 2:
        payload["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0,
                           "showGrid": True}
    else:
        payload["view3D"] = {
            "projection": "parallel", "camera": None,
            "displayMode": "surface", "visibleMaterialIds": [10, 30],
            "opacityByMaterial": {"10": 1.0, "30": 1.0},
            "clip": {"enabled": False, "axis": "x", "position": 0.0,
                     "keep": "positive"},
            "section": {"enabled": False, "axis": "y", "position": 0.0},
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dimension", type=int, default=3)
    parser.add_argument("--delta", type=float, required=True)
    parser.add_argument("--extent", type=float, default=1.0)
    parser.add_argument("--oxide", type=float, required=True)
    parser.add_argument("--orientation", default="Si100")
    parser.add_argument("--time-hours", type=float, required=True)
    parser.add_argument("--time-step-hours", type=float, required=True)
    parser.add_argument("--advanced", default=None,
                        help="JSON object for steps[].advancedOxidation")
    args = parser.parse_args()

    ps.Logger.setLogLevel(ps.LogLevel.ERROR)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(args.dimension)
    advanced = json.loads(args.advanced) if args.advanced else None
    payload = project(args.dimension, delta=args.delta, extent=args.extent,
                      oxide=args.oxide, orientation=args.orientation,
                      time_hours=args.time_hours,
                      time_step_hours=args.time_step_hours, advanced=advanced)
    parsed, _warnings = validate_project(payload)
    normalized = parsed.model_dump(mode="json", exclude_none=True)
    step = normalized["steps"][0]

    if args.dimension == 2:
        domain = ps.Domain(args.delta, args.extent, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
    else:
        domain = ps.Domain(args.delta, args.extent, args.extent,
                           ps.BoundaryType.REFLECTIVE_BOUNDARY)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si,
                 addToExisting=False).apply()
    height = 0.0
    for layer in normalized["geometry"]["layers"]:
        height += float(layer["thickness"])
        ps.MakePlane(domain, height=height,
                     material=getattr(ps.Material, layer["material"]),
                     addToExisting=True).apply()

    model, extra, actual, duration = build_specialized_model(
        ps, domain, normalized, step,
        lambda name: getattr(ps.Material, name), {})
    process = ps.Process(domain, model, duration)
    # Match worker/simulate.py: pin CPU_DISK before apply() so the native
    # Process does not attempt CUDA autodetection on a CPU-only evidence run.
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = 0.25
    process.setParameters(advection)
    for item in extra:
        process.setParameters(item)
    process.apply()

    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                 minNodeDistanceFactor=0.01)
    data = mesh.getCellData()
    materials = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            materials = [int(value) for value in data.getScalarData(index)]
    points = [[round(float(value), 9) for value in point]
              for point in mesh.getNodes()]
    digest = hashlib.sha256(json.dumps([points, materials],
                                       separators=(",", ":")).encode()).hexdigest()
    print(json.dumps({
        "dimension": args.dimension,
        "orientation": args.orientation,
        "pointCount": len(points),
        "topology": "triangles" if args.dimension == 3 else "lines",
        "cellCount": len(mesh.getTriangles()) if args.dimension == 3 else len(mesh.getLines()),
        "surfaceHash": digest,
        "advancedOxidation": actual.get("advancedOxidation"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
