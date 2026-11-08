"""Bounded FaradayCageEtching 2D/3D probe driven by the real worker model builder.

Each case runs in its own process with a wall-clock timeout, so a native hang
is reported as ``timeout`` instead of blocking the whole sweep. The case body
reuses the production pieces:

* ``backend.specialized_models.specialized_catalog`` supplies the exact native
  defaults the API/UI materialise;
* ``worker.specialized.build_specialized_model`` constructs the real
  ``FaradayCageEtching``/``FaradayCageParameters`` object;
* ``worker.simulate.configure_rays``/``configure_advection`` apply the same
  schema-7 process parameters as a bounded run;
* ``worker.simulate``'s 2D/3D domain construction (keyword boundary form) is
  mirrored so the probe cannot accidentally use a different overload.

Usage::

    # one case, prints one JSON line
    python -m worker.probe_faraday_2d --case '{"dimension": 2, "cageAngle": 45, "tiltAngle": 30}'

    # whole bounded matrix, spawns one process per case with a 60 s timeout
    python -m worker.probe_faraday_2d --matrix safe-2d --timeout 60 --out var/probe/safe-2d.json

Matrices are defined in ``MATRICES`` below and are intentionally bounded: the
cage/tilt grid, the key IBE parameters at representative anchors, geometry /
grid / duration / ray variants, plus a GPU and a 3D control sweep.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CAGE_ANGLES = (0, 0.2, 1, 5, 10, 30, 45, 60, 75, 89, 90, 120, 180)
TILT_ANGLES = (-90, -60, -30, 0, 30, 60, 90, 120, 180)
IBE_OVERRIDES = (
    {"meanEnergy": 50.0},
    {"meanEnergy": 25.0},
    {"meanEnergy": 1000.0},
    {"sigmaEnergy": 0.0},
    {"sigmaEnergy": 1e-06},
    {"sigmaEnergy": 500.0},
    {"thresholdEnergy": 0.0},
    {"thresholdEnergy": 100.0},
    {"exponent": 0.0},
    {"exponent": 1.0},
    {"exponent": 1000.0},
    {"n_l": 0.0},
    {"n_l": 1000.0},
    {"thetaRMin": 0.0, "thetaRMax": 180.0},
    {"thetaRMin": 90.0, "thetaRMax": 90.0},
    {"minAngle": 0.0, "inflectAngle": 0.0},
    {"minAngle": 180.0, "inflectAngle": 180.0},
    {"planeWaferRate": 0.0},
    {"planeWaferRate": 1.0},
    {"redepositionRate": 1.0, "redepositionThreshold": 0.0},
    {"redepositionRate": 0.0, "redepositionThreshold": 1.0},
    {"cos4YieldA1": 1.0, "cos4YieldA2": 1.0, "cos4YieldA3": 1.0, "cos4YieldA4": 1.0},
)
IBE_ANCHORS = ((0.2, 0.0), (0.2, 30.0), (45.0, 30.0), (90.0, 0.0), (90.0, 60.0))
GPU_CAGE_ANGLES = (0, 0.2, 5, 10, 30, 45, 60, 75, 89, 90)
GPU_TILT_ANGLES = (-30, 0, 15, 30, 45, 60, 90)
THREE_D_CAGE_ANGLES = (0, 0.2, 45, 90)
THREE_D_TILT_ANGLES = (0, 30, 60)


def _stack_project(dimension: int, case: dict[str, Any]) -> dict[str, Any]:
    """Materialise a schema-7 project without the dimension capability gate.

    ``backend.validation.validate_project`` rejects 2D faradayCage until this
    probe justifies opening it, so the probe normalises through ``ProjectV7``
    instead. Every other default (including the IBE block) comes from the
    catalog that the API serves.
    """
    from backend.models import ProjectV7
    from backend.specialized_models import specialized_catalog

    catalog = specialized_catalog()
    spec = next(row for row in catalog["models"] if row["id"] == "faradayCage")
    geometry_kind = str(case.get("geometry", "stack"))
    if geometry_kind == "stack":
        geometry: dict[str, Any] = {
            "kind": "stack", "substrateMaterial": "Si", "baseHeight": 0.0,
            "layers": [
                {"id": "nitride", "name": "Nitride", "material": "Si3N4",
                 "thickness": 0.4, "role": "mask"},
                {"id": "oxide", "name": "Oxide", "material": "SiO2",
                 "thickness": 0.4, "role": "film"},
            ],
            "pattern": {"kind": "none"},
        }
    elif geometry_kind == "plane":
        geometry = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    elif geometry_kind == "trench":
        geometry = {"kind": "trench", "width": 1.0, "depth": 2.0, "taperAngle": 0.0,
                    "maskHeight": 0.4, "maskTaperAngle": 0.0,
                    "substrateMaterial": "Si", "maskMaterial": "Si3N4"}
    else:
        raise ValueError(f"unknown probe geometry {geometry_kind!r}")

    native_parameters: dict[str, Any] = {}
    for field in spec["fields"]:
        if field["kind"] == "materialRates":
            native_parameters[field["key"]] = {}
        else:
            native_parameters[field["key"]] = deepcopy(field["default"])
    native_parameters["cageAngle"] = float(case.get("cageAngle", native_parameters["cageAngle"]))
    native_parameters["tiltAngle"] = float(case.get("tiltAngle", native_parameters["tiltAngle"]))
    for key, value in dict(case.get("ibe") or {}).items():
        if key not in native_parameters:
            raise ValueError(f"unknown IBE override {key!r}")
        native_parameters[key] = value
    if case.get("materialPlaneWaferRateByMaterial") is not None:
        native_parameters["materialPlaneWaferRateByMaterial"] = dict(
            case["materialPlaneWaferRateByMaterial"])

    grid = float(case.get("gridDelta", 0.2))
    x_extent = float(case.get("xExtent", 4.0))
    y_extent = float(case.get("yExtent", 4.0))
    boundary = str(case.get("boundary", "periodic"))
    domain = {"gridDelta": grid, "xExtent": x_extent}
    if dimension == 2:
        domain["xBoundary"] = boundary
    else:
        domain["yExtent"] = y_extent
        domain["boundaryXY"] = boundary

    step = {
        "id": "probe_faraday", "name": "Faraday probe", "enabled": True,
        "model": "specialized", "nativeModel": "faradayCage", "operation": "etch",
        "nativeParameters": native_parameters,
        "duration": float(case.get("duration", 0.02)),
    }
    project: dict[str, Any] = {
        "schemaVersion": 7, "name": "Faraday 2D capability probe",
        "dimension": dimension, "units": {
            "length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": domain, "geometry": geometry, "steps": [step],
        "solver": {
            "timeStepRatio": 0.25, "raysPerPoint": int(case.get("raysPerPoint", 32)),
            "cpuThreads": int(case.get("cpuThreads", 1)),
            "random": {"mode": "fixed", "seed": 271828},
            "enginePreference": "cpu", "timeoutSeconds": 60,
            "advection": {
                "spatialScheme": "ENGQUIST_OSHER_1ST_ORDER",
                "temporalScheme": "FORWARD_EULER",
                "adaptiveTimeStepping": False, "adaptiveTimeStepSubdivisions": 20,
                "calculateIntermediateVelocities": False, "checkDissipation": True,
                "dissipationAlpha": 1.0, "ignoreVoids": False, "velocityOutput": False,
            },
            "rayTracing": {
                "diskRadius": 0.0, "ignoreFluxBoundaries": False, "maxBoundaryHits": 80,
                "maxReflections": 3, "minNodeDistanceFactor": 0.05,
                "normalizationType": "SOURCE", "smoothingNeighbors": 1,
            },
        },
        "outputSettings": {"levelSets": False, "levelSetMeshes": False,
                           "volumeMesh": False, "nativeMetadata": False},
    }
    if dimension == 2:
        project["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    else:
        project["view3D"] = {
            "projection": "parallel", "camera": None, "displayMode": "surface",
            "visibleMaterialIds": [0, 10, 30],
            "opacityByMaterial": {"0": 1.0, "10": 1.0, "30": 1.0},
            "clip": {"enabled": False, "axis": "x", "position": 0.0, "keep": "positive"},
            "section": {"enabled": False, "axis": "y", "position": 0.0},
        }
    normalized = ProjectV7.model_validate(project).model_dump(mode="json", exclude_none=True)
    return normalized


def _make_native_domain(ps: Any, project: dict[str, Any]) -> Any:
    """Mirror ``worker/simulate.execute`` domain + geometry construction."""
    from worker.simulate import material_handle

    dimension = int(project["dimension"])
    domain_request = project["domain"]
    boundary = (ps.BoundaryType.REFLECTIVE_BOUNDARY
                if domain_request.get("xBoundary", domain_request.get("boundaryXY")) == "reflective"
                else ps.BoundaryType.PERIODIC_BOUNDARY)
    if dimension == 2:
        # Keyword form: 4.7.0 binds three positionals as (grid, xExtent, yExtent).
        domain = ps.Domain(float(domain_request["gridDelta"]),
                           float(domain_request["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(domain_request["gridDelta"]),
                           float(domain_request["xExtent"]),
                           float(domain_request["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(domain, height=float(geometry["height"]),
                     material=material_handle(ps, geometry["substrateMaterial"]),
                     addToExisting=False).apply()
    elif geometry["kind"] == "trench":
        ps.MakeTrench(
            domain=domain, trenchWidth=float(geometry["width"]),
            trenchDepth=float(geometry["depth"]),
            trenchTaperAngle=float(geometry["taperAngle"]),
            maskHeight=float(geometry["maskHeight"]),
            maskTaperAngle=float(geometry["maskTaperAngle"]), halfTrench=False,
            material=material_handle(ps, geometry["substrateMaterial"]),
            maskMaterial=material_handle(ps, geometry["maskMaterial"])).apply()
    elif geometry["kind"] == "stack":
        ps.MakePlane(domain, height=float(geometry["baseHeight"]),
                     material=material_handle(ps, geometry["substrateMaterial"]),
                     addToExisting=False).apply()
        height = float(geometry["baseHeight"])
        for layer in geometry["layers"]:
            height += float(layer["thickness"])
            ps.MakePlane(domain, height=height,
                         material=material_handle(ps, layer["material"]),
                         addToExisting=True).apply()
    else:
        raise ValueError(f"probe does not construct geometry {geometry['kind']!r}")
    return domain


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """Execute one real native Faraday case and return a JSON summary."""
    import viennaps as ps

    from backend.specialized_models import model_spec
    from worker.simulate import (MATERIAL_IDS, configure_advection, configure_rays,
                                 material_handle)
    from worker.specialized import build_specialized_model

    bypass_gate = bool(case.get("probeBypassSafetyGate"))
    if bypass_gate:
        # Only the GPU/engine-characterisation matrices set this. The production
        # gate stays untouched; the probe patches the symbol in this child
        # process so the known-hanging native kernel can be measured instead of
        # rejected. Safe-matrix cases never take this path.
        import worker.specialized as specialized_module
        specialized_module.faraday_cage_2d_engine_error = lambda flux_engine: None

    dimension = int(case.get("dimension", 2))
    engine = str(case.get("engine", "CPU_DISK"))
    project = _stack_project(dimension, case)
    step = project["steps"][0]

    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")

    domain = _make_native_domain(ps, project)
    before_mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                        minNodeDistanceFactor=0.01)
    vertical = 1 if dimension == 2 else 2
    before_top = max(float(point[vertical]) for point in before_mesh.getNodes())

    model, extra, actual, duration = build_specialized_model(
        ps, domain, project, step, lambda name: material_handle(ps, name), MATERIAL_IDS,
        flux_engine=engine)
    if model_spec(step["nativeModel"]) is None:
        raise AssertionError("catalog lost the faradayCage row")
    if type(model).__name__ != "FaradayCageEtching":
        raise AssertionError(f"unexpected model {type(model).__name__}")

    process = ps.Process(domain, model, duration)
    if engine == "default":
        pass
    elif engine == "AUTO":
        process.setFluxEngineType(ps.FluxEngineType.AUTO)
    else:
        process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    rays, _ray_actual = configure_rays(ps, project, 271828)
    process.setParameters(rays)
    advection, _advection_actual = configure_advection(ps, project)
    process.setParameters(advection)
    for item in extra:
        process.setParameters(item)

    started = time.time()
    process.apply()
    elapsed = time.time() - started

    after_mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False,
                                       minNodeDistanceFactor=0.01)
    after_top = max(float(point[vertical]) for point in after_mesh.getNodes())
    level_set_points = [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()]
    container = actual.get("nativeParameterContainer", {})
    return {
        "ok": True,
        "dimension": dimension,
        "engine": engine,
        "cageAngle": float(step["nativeParameters"]["cageAngle"]),
        "tiltAngle": float(step["nativeParameters"]["tiltAngle"]),
        "geometry": project["geometry"]["kind"],
        "gridDelta": float(project["domain"]["gridDelta"]),
        "duration": float(duration),
        "nativeModule": type(model).__module__,
        "nativeClass": type(model).__name__,
        "cageAngleReadback": container.get("cageAngle"),
        "tiltAngleReadback": container.get("ibeParameters", {}).get("tiltAngle"),
        "beforeTop": before_top,
        "afterTop": after_top,
        "etchedDepth": before_top - after_top,
        "levelSetPoints": level_set_points,
        "emptyLevelSet": any(count <= 0 for count in level_set_points),
        "safetyGateBypassed": bypass_gate,
        "applySeconds": round(elapsed, 4),
    }


def _cases_for(matrix: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    if matrix == "safe-2d":
        for cage in CAGE_ANGLES:
            for tilt in TILT_ANGLES:
                cases.append({"dimension": 2, "cageAngle": cage, "tiltAngle": tilt,
                              "engine": "CPU_DISK"})
        for cage, tilt in IBE_ANCHORS:
            for override in IBE_OVERRIDES:
                cases.append({"dimension": 2, "cageAngle": cage, "tiltAngle": tilt,
                              "engine": "CPU_DISK", "ibe": override})
        for geometry in ("stack", "plane", "trench"):
            for grid in (0.1, 0.2, 0.3):
                cases.append({"dimension": 2, "cageAngle": 45, "tiltAngle": 30,
                              "engine": "CPU_DISK", "geometry": geometry, "gridDelta": grid})
        for duration in (1.0, 5.0):
            cases.append({"dimension": 2, "cageAngle": 0.2, "tiltAngle": 0,
                          "engine": "CPU_DISK", "duration": duration})
            cases.append({"dimension": 2, "cageAngle": 90, "tiltAngle": 60,
                          "engine": "CPU_DISK", "duration": duration})
        for rays in (16, 128):
            cases.append({"dimension": 2, "cageAngle": 30, "tiltAngle": 30,
                          "engine": "CPU_DISK", "raysPerPoint": rays})
        for threads in (2, 4):
            cases.append({"dimension": 2, "cageAngle": 30, "tiltAngle": 30,
                          "engine": "CPU_DISK", "cpuThreads": threads})
        cases.append({"dimension": 2, "cageAngle": 45, "tiltAngle": 30,
                      "engine": "CPU_DISK", "boundary": "reflective"})
        for rates in ({"SiO2": 0.5}, {"Si": 0.5}, {"Si3N4": 0.01}):
            cases.append({"dimension": 2, "cageAngle": 45, "tiltAngle": 30,
                          "engine": "CPU_DISK",
                          "materialPlaneWaferRateByMaterial": rates})
    elif matrix == "gpu-2d":
        for cage in GPU_CAGE_ANGLES:
            for tilt in GPU_TILT_ANGLES:
                cases.append({"dimension": 2, "cageAngle": cage, "tiltAngle": tilt,
                              "engine": "GPU_DISK", "probeBypassSafetyGate": True})
    elif matrix == "control-3d":
        for cage in THREE_D_CAGE_ANGLES:
            for tilt in THREE_D_TILT_ANGLES:
                cases.append({"dimension": 3, "cageAngle": cage, "tiltAngle": tilt,
                              "engine": "CPU_DISK"})
        for override in ({"meanEnergy": 50.0}, {"sigmaEnergy": 0.0}, {"exponent": 0.0},
                         {"thetaRMin": 0.0, "thetaRMax": 180.0},
                         {"planeWaferRate": 0.0}):
            cases.append({"dimension": 3, "cageAngle": 45, "tiltAngle": 30,
                          "engine": "CPU_DISK", "ibe": override})
        for grid in (0.1, 0.3):
            cases.append({"dimension": 3, "cageAngle": 45, "tiltAngle": 30,
                          "engine": "CPU_DISK", "gridDelta": grid})
        cases.append({"dimension": 3, "cageAngle": 45, "tiltAngle": 30,
                      "engine": "CPU_DISK", "duration": 1.0})
    elif matrix == "engine-control-2d":
        for cage, tilt in ((0.2, 0.0), (0.2, 30.0), (45.0, 30.0), (60.0, 60.0), (90.0, 0.0)):
            for engine in ("AUTO", "GPU_DISK"):
                cases.append({"dimension": 2, "cageAngle": cage, "tiltAngle": tilt,
                              "engine": engine, "probeBypassSafetyGate": True})
    else:
        raise ValueError(f"unknown matrix {matrix!r}")
    return cases


def _driver(matrix: str, timeout: float, out: Path | None) -> int:
    cases = _cases_for(matrix)
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        command = [sys.executable, "-m", "worker.probe_faraday_2d",
                   "--case", json.dumps(case, sort_keys=True)]
        started = time.time()
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True,
                                       text=True, timeout=timeout)
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = None
            stdout = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            timed_out = True
        wall = time.time() - started
        if timed_out:
            status = "timeout"
        elif returncode == 0:
            status = "ok"
        elif returncode is not None and returncode < 0:
            # Negative return code: killed by a signal (native abort/SIGSEGV).
            status = "crash"
        else:
            status = "error"
        record: dict[str, Any] = {"case": case, "status": status,
                                  "returncode": returncode, "wallSeconds": round(wall, 3)}
        summary: dict[str, Any] | None = None
        if status == "ok":
            import re
            clean = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
            for line in reversed(clean.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        summary = json.loads(line)
                    except ValueError:
                        continue
                    break
            if summary is None:
                status = record["status"] = "error"
                record["error"] = "probe produced no JSON summary"
        else:
            tail = (stderr or stdout).strip().splitlines()[-4:]
            record["error"] = "\n".join(tail)
        if summary is not None:
            record.update(summary)
        results.append(record)
        print(f"[{index}/{len(cases)}] {status:7s} "
              f"{case.get('dimension')}D cage={case.get('cageAngle')} "
              f"tilt={case.get('tiltAngle')} engine={case.get('engine')} "
              f"{'ibe=' + json.dumps(case['ibe'], sort_keys=True) if case.get('ibe') else ''} "
              f"({record['wallSeconds']}s)", flush=True)
    payload = {"matrix": matrix, "timeoutSeconds": timeout,
               "caseCount": len(results), "results": results}
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = {status: sum(1 for row in results if row["status"] == status)
              for status in ("ok", "error", "crash", "timeout")}
    print(json.dumps({"matrix": matrix, "counts": counts}, sort_keys=True))
    return 0 if counts["crash"] == 0 and counts["timeout"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=str, help="single case JSON")
    parser.add_argument("--matrix", type=str,
                        choices=sorted({"safe-2d", "gpu-2d", "control-3d", "engine-control-2d"}))
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="per-case wall-clock timeout in seconds (default 60)")
    parser.add_argument("--out", type=Path, help="write the full matrix result JSON here")
    args = parser.parse_args()
    if args.case:
        try:
            summary = run_case(json.loads(args.case))
        except BaseException as exc:  # report the native failure instead of a traceback-only exit
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
            traceback.print_exc()
            return 1
        print(json.dumps(summary, sort_keys=True), flush=True)
        return 0
    if args.matrix:
        return _driver(args.matrix, args.timeout, args.out)
    parser.error("either --case or --matrix is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
