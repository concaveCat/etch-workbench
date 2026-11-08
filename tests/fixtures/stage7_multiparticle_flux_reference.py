"""Independent direct Process.calculateFlux mesh for one MultiParticle step."""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import sys
from pathlib import Path

import viennaps as ps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage7_multiparticle_reference import (
    add_species, direct_material, make_domain, project_material_names, register_custom_materials,
)


def arrays(data):
    scalars = {str(data.getScalarDataLabel(i)): [float(value) for value in data.getScalarData(i)]
               for i in range(data.getScalarDataSize())}
    vectors = {str(data.getVectorDataLabel(i)): [[float(value) for value in row] for row in data.getVectorData(i)]
               for i in range(data.getVectorDataSize())}
    return {"scalars": scalars, "vectors": vectors}


def main(project_path, step_id, output_path, engine="CPU_DISK", initial_state_path=None):
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row["id"] == step_id)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    register_custom_materials(project.get("materialDefinitions", []))
    if initial_state_path:
        settings = project["domain"]
        boundary_name = settings.get("xBoundary", settings.get("boundaryXY"))
        boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
        if project["dimension"] == 2:
            domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), boundary=boundary)
        else:
            domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]),
                               float(settings["yExtent"]), boundary)
        ps.Reader(domain, str(Path(initial_state_path).resolve())).apply()
        state_info = {"sha256": hashlib.sha256(Path(initial_state_path).read_bytes()).hexdigest(),
                      "loadedWith": "viennaps.Reader",
                      "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i))
                                                       for i in range(domain.getMaterialMap().size())],
                      "levelSetCount": int(domain.getNumberOfLevelSets())}
    else:
        domain = make_domain(project)
        state_info = None
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(direct_material(step["depositMaterial"]))
    model = ps.MultiParticleProcess()
    labels = add_species(model, step)
    relation = step["rateCoefficients"]
    defaults = relation["defaultBySpecies"]
    overrides = relation.get("byMaterial", {})
    material_names = {str(direct_material(name)): name for name in project_material_names(project)}
    sign = -1.0 if step["operation"] == "etch" else 1.0

    def rate_function(fluxes, material):
        name = material_names.get(str(material))
        if name is None:
            raise RuntimeError(f"unknown callback material {material}")
        coefficients = overrides.get(name, {})
        return sign * sum(float(fluxes[index]) * float(coefficients.get(label, defaults[label]))
                          for index, label in enumerate(labels))

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    ray = ps.RayTracingParameters()
    requested = project["solver"].get("rayTracing", {})
    ray.raysPerPoint = int(project["solver"]["raysPerPoint"])
    ray.rngSeed = int(project["solver"]["random"]["seed"])
    ray.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                "minNodeDistanceFactor", "smoothingNeighbors"):
        if key in requested:
            setattr(ray, key, requested[key])
    if "normalizationType" in requested:
        ray.normalizationType = getattr(ps.NormalizationType, requested["normalizationType"])
    process.setParameters(ray)
    mesh = process.calculateFlux()
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    cells = {"lines": [[int(value) for value in row] for row in mesh.getLines()],
             "triangles": [[int(value) for value in row] for row in mesh.getTriangles()],
             "tetras": [[int(value) for value in row] for row in mesh.getTetras()],
             "hexas": [[int(value) for value in row] for row in mesh.getHexas()],
             "vertices": [[int(value) for value in row] for row in mesh.getVerticies()]}
    topology = {key: value for key, value in cells.items() if value}
    point_data = arrays(mesh.getPointData())
    cell_data = arrays(mesh.getCellData())
    fields = []
    for association, data in (("pointData", point_data), ("cellData", cell_data)):
        for kind in ("scalars", "vectors"):
            for name, values in data[kind].items():
                fields.append({"name": name, "association": association, "components": 1 if kind == "scalars" else 3,
                               "tuples": len(values), "nativeUnit": None,
                               "unitStatus": "not-provided-by-native-mesh-interface"})
    result = {"viennapsModulePath": ps.__file__, "versions": {"viennaps": importlib.metadata.version("viennaps"),
                           "viennals": importlib.metadata.version("viennals")},
              "dimension": project["dimension"], "stepId": step_id, "engine": engine, "points": points,
              "topology": topology, "pointData": point_data, "cellData": cell_data, "fields": fields,
              "ray": ray.toMetaData(), "speciesOrder": labels}
    if state_info:
        result["loadedNativeState"] = state_info
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"versions": result["versions"], "dimension": result["dimension"],
                      "points": len(points), "topology": {key: len(rows) for key, rows in topology.items()},
                      "fieldNames": [row["name"] for row in fields], "speciesOrder": labels}), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
