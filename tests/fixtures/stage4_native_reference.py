"""Independent ViennaPS 4.6.2 solver/flux reference; invoke via bounded_child.py."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from importlib.metadata import version
from pathlib import Path

import viennaps as ps


def mesh_arrays(data):
    scalars = {str(data.getScalarDataLabel(i)): [float(value) for value in data.getScalarData(i)]
               for i in range(data.getScalarDataSize())}
    vectors = {str(data.getVectorDataLabel(i)): [[float(value) for value in row] for row in data.getVectorData(i)]
               for i in range(data.getVectorDataSize())}
    return {"scalars": scalars, "vectors": vectors}


def report_mesh(mesh):
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    cells = {"lines": [[int(value) for value in row] for row in mesh.getLines()],
             "triangles": [[int(value) for value in row] for row in mesh.getTriangles()],
             "tetras": [[int(value) for value in row] for row in mesh.getTetras()],
             "hexas": [[int(value) for value in row] for row in mesh.getHexas()],
             "vertices": [[int(value) for value in row] for row in mesh.getVerticies()]}
    topology = {name: value for name, value in cells.items() if value}
    point_data = mesh_arrays(mesh.getPointData())
    cell_data = mesh_arrays(mesh.getCellData())
    fields = []
    for association, data in (("pointData", point_data), ("cellData", cell_data)):
        for kind in ("scalars", "vectors"):
            for name, values in data[kind].items():
                fields.append({"name": name, "association": association,
                               "components": 1 if kind == "scalars" else 3,
                               "tuples": len(values), "nativeUnit": None,
                               "unitStatus": "not-provided-by-native-mesh-interface"})
    identity = hashlib.sha256(json.dumps({"points": points, "topology": topology}, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return {"points": points, "topology": topology, "pointData": point_data,
            "cellData": cell_data, "fields": fields, "meshIdentity": identity}


def build_domain(project):
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    boundary = (ps.BoundaryType.REFLECTIVE_BOUNDARY
                if project["domain"].get("xBoundary", project["domain"].get("boundaryXY")) == "reflective"
                else ps.BoundaryType.PERIODIC_BOUNDARY)
    if dimension == 2:
        domain = ps.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]),
                           float(project["domain"]["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] != "plane":
        raise ValueError("Stage 4 direct reference fixture uses a plane to isolate native solver behavior")
    material = ps.Material.Si if geometry["substrateMaterial"] == "Si" else ps.MaterialRegistry.instance().getMaterial(geometry["substrateMaterial"])
    ps.MakePlane(domain, height=float(geometry["height"]), material=material, addToExisting=False).apply()
    return domain


def apply_advection(parameters, settings):
    parameters.timeStepRatio = float(settings["timeStepRatio"])
    for name in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                 "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
        setattr(parameters, name, settings[name])
    parameters.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, settings["spatialScheme"])
    parameters.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, settings["temporalScheme"])


def apply_rays(parameters, solver):
    settings = solver["rayTracing"]
    parameters.raysPerPoint = int(solver["raysPerPoint"])
    parameters.rngSeed = int(solver["random"]["seed"])
    parameters.useRandomSeeds = False
    for name in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                 "minNodeDistanceFactor", "smoothingNeighbors"):
        setattr(parameters, name, settings[name])
    parameters.normalizationType = getattr(ps.NormalizationType, settings["normalizationType"])


def main():
    request_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    project = request["project"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(output_path.parent)
    domain = build_domain(project)
    step = project["steps"][0]
    mode = request["mode"]
    if mode == "flux":
        rate = float(step["rateMagnitude"]) * (-1.0 if step["operation"] == "etch" else 1.0)
        model = ps.SingleParticleProcess(rate=rate, stickingProbability=float(step["stickingProbability"]),
                                         sourceExponent=float(step["sourceExponent"]), maskMaterials=[])
        process = ps.Process(domain, model, float(step["duration"]))
        engine = request.get("engine", "CPU_DISK")
        process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
        rays = ps.RayTracingParameters()
        apply_rays(rays, project["solver"])
        process.setParameters(rays)
        flux_mesh = process.calculateFlux()
        report = report_mesh(flux_mesh)
        native_vtp = output_path.parent / "direct-flux.vtp"
        ps.ls.VTKWriter(flux_mesh, ps.ls.FileFormatEnum.VTP, str(native_vtp)).apply()
        result = {"mode": mode, "flux": report, "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
                  "engine": engine, "seed": int(project["solver"]["random"]["seed"]),
                  "threads": int(project["solver"]["cpuThreads"]), "actualRate": rate,
                  "nativeVtp": {"fileName": native_vtp.name, "sizeBytes": native_vtp.stat().st_size,
                                "sha256": hashlib.sha256(native_vtp.read_bytes()).hexdigest()},
                  "rayReadback": {name: getattr(rays, name).name if name == "normalizationType" else getattr(rays, name)
                                  for name in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                                               "minNodeDistanceFactor", "normalizationType", "raysPerPoint", "rngSeed",
                                               "smoothingNeighbors", "useRandomSeeds")}}
    elif mode == "process":
        rate = float(step["rateMagnitude"]) * (-1.0 if step["operation"] == "etch" else 1.0)
        model = ps.IsotropicProcess(rate=rate, maskMaterials=[])
        process = ps.Process(domain, model, float(step["duration"]))
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        advection = ps.AdvectionParameters()
        apply_advection(advection, project["solver"]["advection"] | {"timeStepRatio": project["solver"]["timeStepRatio"]})
        process.setParameters(advection)
        process.setIntermediateOutputPath(str(output_path.parent / "native-intermediate"))
        (output_path.parent / "native-intermediate").mkdir(exist_ok=True)
        process.apply()
        surface = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        outputs = []
        for path in sorted(output_path.parent.glob("*.vtp")):
            outputs.append({"fileName": path.name, "sizeBytes": path.stat().st_size,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        result = {"mode": mode, "surface": report_mesh(surface), "outputs": outputs,
                  "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
                  "advectionReadback": {"spatialScheme": advection.spatialScheme.name,
                                        "temporalScheme": advection.temporalScheme.name,
                                        "adaptiveTimeStepping": bool(advection.adaptiveTimeStepping),
                                        "adaptiveTimeStepSubdivisions": int(advection.adaptiveTimeStepSubdivisions),
                                        "calculateIntermediateVelocities": bool(advection.calculateIntermediateVelocities),
                                        "checkDissipation": bool(advection.checkDissipation),
                                        "dissipationAlpha": float(advection.dissipationAlpha),
                                        "ignoreVoids": bool(advection.ignoreVoids),
                                        "velocityOutput": bool(advection.velocityOutput),
                                        "timeStepRatio": float(advection.timeStepRatio)}}
    else:
        raise ValueError("mode must be flux or process")
    output_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    print(json.dumps({"mode": mode, "versions": result["versions"], "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
