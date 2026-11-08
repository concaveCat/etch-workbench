"""Locate CPU MultiParticle non-finite fluxes on a saved native Domain.

This is a diagnostic only: invalid callback velocities are replaced with zero
so calculateFlux can return its native mesh for inspection. Such a result must
never be used as a product simulation or acceptance result.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
import sys
from collections import Counter
from pathlib import Path

import viennaps as ps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage7_multiparticle_reference import add_species, direct_material, project_material_names


def safe(value):
    number = float(value)
    return number if math.isfinite(number) else None


def mesh_arrays(data):
    scalars = {}
    vectors = {}
    for index in range(data.getScalarDataSize()):
        scalars[str(data.getScalarDataLabel(index))] = [safe(value) for value in data.getScalarData(index)]
    for index in range(data.getVectorDataSize()):
        vectors[str(data.getVectorDataLabel(index))] = [
            [safe(component) for component in row] for row in data.getVectorData(index)
        ]
    return {"scalars": scalars, "vectors": vectors}


def main(project_path: Path, state_path: Path, output_path: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row.get("enabled"))
    solver, domain_settings = project["solver"], project["domain"]
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(solver["cpuThreads"]))
    boundary_name = domain_settings.get("xBoundary", domain_settings.get("boundaryXY"))
    boundary = (ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective"
                else ps.BoundaryType.PERIODIC_BOUNDARY)
    if project["dimension"] == 2:
        domain = ps.Domain(float(domain_settings["gridDelta"]), float(domain_settings["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(domain_settings["gridDelta"]), float(domain_settings["xExtent"]),
                           float(domain_settings["yExtent"]), boundary)
    ps.Reader(domain, str(state_path.resolve())).apply()

    labels = add_species((model := ps.MultiParticleProcess()), step)
    relation = step["rateCoefficients"]
    defaults = relation["defaultBySpecies"]
    overrides = relation.get("byMaterial", {})
    material_names = {str(direct_material(name)): name for name in project_material_names(project)}
    callback = {"count": 0, "nonFiniteCount": 0, "nonFiniteByMaterial": Counter(), "examples": []}
    sign = -1.0 if step["operation"] == "etch" else 1.0

    def rate_function(fluxes, material):
        name = material_names.get(str(material), str(material))
        values = [float(value) for value in fluxes]
        callback["count"] += 1
        if len(values) != len(labels) or any(not math.isfinite(value) for value in values):
            callback["nonFiniteCount"] += 1
            callback["nonFiniteByMaterial"][name] += 1
            if len(callback["examples"]) < 32:
                callback["examples"].append({
                    "callbackIndex": callback["count"] - 1,
                    "material": name,
                    "fluxes": [safe(value) for value in values],
                    "vectorLength": len(values),
                })
            return 0.0
        coefficients = overrides.get(name, {})
        return sign * sum(values[index] * float(coefficients.get(label, defaults[label]))
                          for index, label in enumerate(labels))

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    ray = ps.RayTracingParameters()
    ray.raysPerPoint = int(solver["raysPerPoint"])
    ray.rngSeed = int(solver["random"]["seed"])
    ray.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                "minNodeDistanceFactor", "smoothingNeighbors"):
        if key in solver.get("rayTracing", {}):
            setattr(ray, key, solver["rayTracing"][key])
    if "normalizationType" in solver.get("rayTracing", {}):
        ray.normalizationType = getattr(ps.NormalizationType, solver["rayTracing"]["normalizationType"])
    process.setParameters(ray)
    mesh = process.calculateFlux()
    points = [[safe(value) for value in row] for row in mesh.getNodes()]
    fields = {}
    nonfinite_field_points = []
    for association, data in (("pointData", mesh.getPointData()), ("cellData", mesh.getCellData())):
        values = mesh_arrays(data)
        fields[association] = values
        for kind in ("scalars", "vectors"):
            for name, rows in values[kind].items():
                for index, row in enumerate(rows):
                    components = row if isinstance(row, list) else [row]
                    if any(value is None for value in components):
                        item = {"association": association, "kind": kind, "name": name, "index": index}
                        if association == "pointData" and index < len(points):
                            item["point"] = points[index]
                        nonfinite_field_points.append(item)
    topology = {
        "lines": [[int(value) for value in row] for row in mesh.getLines()],
        "triangles": [[int(value) for value in row] for row in mesh.getTriangles()],
    }
    output = {
        "status": "diagnostic-only",
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "modulePath": ps.__file__, "engine": "CPU_DISK", "dimension": project["dimension"],
        "stepId": step["id"], "raysPerPoint": int(solver["raysPerPoint"]),
        "loadedState": str(state_path), "speciesOrder": labels, "callback": callback,
        "mesh": {"points": points, "topology": topology, "fields": fields,
                 "nonFiniteFieldEntries": nonfinite_field_points},
        "note": "Non-finite callback values were replaced by zero only to extract diagnostics; this is not physical output.",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({"status": output["status"], "callbackCount": callback["count"],
                      "nonFiniteCallbackCount": callback["nonFiniteCount"],
                      "nonFiniteFieldEntries": len(nonfinite_field_points),
                      "examples": callback["examples"][:4]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: probe_cpu_multiparticle_flux_nonfinite.py PROJECT.json STATE.vpsd OUTPUT.json")
    main(*(Path(value) for value in sys.argv[1:]))
