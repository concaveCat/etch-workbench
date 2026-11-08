"""Independent ViennaPS reference for schema-7 csvProcess API comparisons.

This file is always run through scripts/bounded_child.py by its parent test. It
never imports workbench worker code: it builds the domain, constructs the native
``CSVFileProcess`` and configures the native advection parameters directly from
the submitted project document.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import viennaps as ps


def material(name: str):
    registry = ps.MaterialRegistry.instance()
    if registry.findMaterial(name) is None:
        raise RuntimeError(f"material {name!r} is not registered")
    return registry.getMaterial(name)


def build_domain(project):
    ps.setDimension(project["dimension"])
    ps.setNumThreads(project["solver"]["cpuThreads"])
    delta = float(project["domain"]["gridDelta"])
    boundary_name = project["domain"].get("boundaryXY", project["domain"].get("xBoundary"))
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if project["dimension"] == 2:
        domain = ps.Domain(delta, float(project["domain"]["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(delta, float(project["domain"]["xExtent"]), float(project["domain"]["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(domain, height=float(geometry["height"]), material=material(geometry["substrateMaterial"]),
                     addToExisting=False).apply()
    elif geometry["kind"] == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=float(geometry["width"]), trenchDepth=float(geometry["depth"]),
                      trenchTaperAngle=float(geometry["taperAngle"]), maskHeight=float(geometry["maskHeight"]),
                      maskTaperAngle=float(geometry["maskTaperAngle"]), halfTrench=False,
                      material=material(geometry["substrateMaterial"]),
                      maskMaterial=material(geometry.get("maskMaterial", "Mask"))).apply()
    else:
        raise ValueError("reference fixture supports plane and trench inputs")
    return domain


def mask_material_names(project):
    geometry = project["geometry"]
    if geometry["kind"] == "trench" and float(geometry["maskHeight"]) > 0:
        return [geometry.get("maskMaterial", "Mask")]
    return []


def snapshot(domain, step_id):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    dim = 2 if not mesh.getTriangles() else 3
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells = [[int(value) for value in cell] for cell in (mesh.getLines() if dim == 2 else mesh.getTriangles())]
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    material_map = [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]
    return {"stepId": step_id, "points": points, "cells": cells,
            "materialIds": material_ids, "materialMap": material_map}


def advection_parameters(project):
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    if int(project["schemaVersion"]) >= 7:
        requested = project["solver"]["advection"]
        advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested["spatialScheme"])
        advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested["temporalScheme"])
        for name in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                     "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
            setattr(advection, name, requested[name])
    return advection


def run_step(domain, project, step, asset_path):
    raw_direction = [float(value) for value in step["direction"]]
    norm = math.sqrt(sum(value * value for value in raw_direction))
    if norm <= 1e-12:
        raise RuntimeError("invalid zero reference direction")
    direction = [value / norm for value in raw_direction]
    masks = [material(name) for name in mask_material_names(project)]
    model = ps.CSVFileProcess(str(asset_path), direction, [float(value) for value in step["offset"]],
                              float(step["isotropicComponent"]), float(step["directionalComponent"]),
                              masks, bool(step["calculateVisibility"]))
    model.setInterpolationMode(step["interpolationMode"])
    if step["interpolationMode"] == "IDW":
        model.setIDWNeighbors(int(step["idwNeighbors"]))
    model.setOffset([float(value) for value in step["offset"]])
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.setParameters(advection_parameters(project))
    process.apply()


def main():
    source = Path(sys.argv[1])
    asset_path = Path(sys.argv[2])
    destination = Path(sys.argv[3])
    project = json.loads(source.read_text(encoding="utf-8"))
    domain = build_domain(project)
    output = {"initial": snapshot(domain, "initial")}
    for step in project["steps"]:
        if not step["enabled"]:
            continue
        if step["model"] != "csvProcess":
            raise RuntimeError("reference fixture only executes csvProcess steps")
        run_step(domain, project, step, asset_path)
        output[step["id"]] = snapshot(domain, step["id"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stepIds": list(output), "dimension": project["dimension"]}))


if __name__ == "__main__":
    main()
