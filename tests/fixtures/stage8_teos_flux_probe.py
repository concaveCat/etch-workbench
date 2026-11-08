"""Independent direct Process.calculateFlux reference for a specialized step."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import viennaps as ps

from stage8_direct_reference import make_domain, make_model, material


def main(project_path: Path, step_id: str, output: Path) -> None:
    project = json.loads(project_path.read_text(encoding="utf-8"))
    step = next(row for row in project["steps"] if row["id"] == step_id)
    model_id = step["nativeModel"]
    dimension = int(project["dimension"])
    domain = make_domain(project)
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(ps.Material.SiGe if model_id == "selectiveEpitaxy" else material(step["depositMaterial"]))
    model, extras = make_model(step, domain, project)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    rays = ps.RayTracingParameters()
    ray_input = project["solver"]["rayTracing"]
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections", "minNodeDistanceFactor", "smoothingNeighbors"):
        setattr(rays, key, ray_input[key])
    rays.normalizationType = getattr(ps.NormalizationType, ray_input["normalizationType"])
    process.setParameters(rays)
    for item in extras: process.setParameters(item)
    mesh = process.calculateFlux()
    point_mesh_data = mesh.getPointData()
    point_data = {"scalars": {str(point_mesh_data.getScalarDataLabel(i)): [float(v) for v in point_mesh_data.getScalarData(i)] for i in range(point_mesh_data.getScalarDataSize())},
                  "vectors": {str(point_mesh_data.getVectorDataLabel(i)): [[float(v) for v in row] for row in point_mesh_data.getVectorData(i)] for i in range(point_mesh_data.getVectorDataSize())}}
    cell_mesh_data = mesh.getCellData()
    cell_data = {"scalars": {str(cell_mesh_data.getScalarDataLabel(i)): [float(v) for v in cell_mesh_data.getScalarData(i)] for i in range(cell_mesh_data.getScalarDataSize())},
                 "vectors": {str(cell_mesh_data.getVectorDataLabel(i)): [[float(v) for v in row] for row in cell_mesh_data.getVectorData(i)] for i in range(cell_mesh_data.getVectorDataSize())}}
    cells = {"lines": [[int(v) for v in row] for row in mesh.getLines()],
             "triangles": [[int(v) for v in row] for row in mesh.getTriangles()],
             "tetras": [[int(v) for v in row] for row in mesh.getTetras()],
             "hexas": [[int(v) for v in row] for row in mesh.getHexas()],
             "vertices": [[int(v) for v in row] for row in mesh.getVerticies()]}
    topology = {name: value for name, value in cells.items() if value}
    points = [[float(v) for v in row] for row in mesh.getNodes()]
    fields = []
    for association, data in (("pointData", point_data), ("cellData", cell_data)):
        for kind in ("scalars", "vectors"):
            for name, values in data[kind].items():
                fields.append({"name": name, "association": association, "components": 1 if kind == "scalars" else 3,
                               "tuples": len(values), "nativeUnit": None,
                               "unitStatus": "not-provided-by-native-mesh-interface"})
    identity_payload = {"points": points, "topology": topology}
    mesh_identity = hashlib.sha256(json.dumps(identity_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    output.write_text(json.dumps({"model": model_id, "dimension": dimension, "engine": "CPU_DISK",
                                  "seed": int(project["solver"]["random"]["seed"]), "pointCount": len(points),
                                  "lineCount": len(cells["lines"]), "triangleCount": len(cells["triangles"]),
                                  "meshIdentity": mesh_identity, "points": points, "topology": topology,
                                  "pointData": point_data, "cellData": cell_data, "fields": fields,
                                  "ray": rays.toMetaData()}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4: raise SystemExit("usage: stage8_teos_flux_probe.py INPUT.json STEP_ID OUTPUT.json")
    main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
