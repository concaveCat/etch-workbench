"""Independent direct ViennaPS reference for recipe geometry operations.

Always run this fixture inside scripts/bounded_child.py.
"""
from __future__ import annotations

import json
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
    domain_settings = project["domain"]
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if domain_settings.get("xBoundary", domain_settings.get("boundaryXY")) == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if project["dimension"] == 2:
        domain = ps.Domain(float(domain_settings["gridDelta"]), float(domain_settings["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(domain_settings["gridDelta"]), float(domain_settings["xExtent"]), float(domain_settings["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(domain, height=float(geometry["height"]), material=material(geometry["substrateMaterial"]), addToExisting=False).apply()
    elif geometry["kind"] == "stack":
        ps.MakePlane(domain, height=float(geometry.get("baseHeight", 0.0)), material=material(geometry["substrateMaterial"]), addToExisting=False).apply()
        height = float(geometry.get("baseHeight", 0.0))
        for layer in geometry["layers"]:
            height += float(layer["thickness"])
            ps.MakePlane(domain, height=height, material=material(layer["material"]), addToExisting=True).apply()
        pattern = geometry["pattern"]
        if pattern["kind"] != "none":
            top = geometry["layers"][-1]
            epsilon = float(domain_settings["gridDelta"]) * 1e-3
            position = [0.0] * project["dimension"]
            position[-1] = height - float(top["thickness"]) - epsilon
            factory = ps.GeometryFactory(domain.getSetup(), "GeometryReferenceStackPattern")
            angle = -float(pattern.get("taperAngle", 0.0))
            if pattern["kind"] == "trench":
                stencil = factory.makeBoxStencil(position, float(pattern["width"]), float(top["thickness"]) + 2 * epsilon, angle)
            else:
                stencil = factory.makeCylinderStencil(position, float(pattern["radius"]), float(top["thickness"]) + 2 * epsilon, angle)
            domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT, False)
    else:
        raise ValueError("reference fixture supports plane and stack inputs")
    return domain


def material_map(domain):
    mapping = domain.getMaterialMap()
    return [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]


def snapshot(domain, step_id, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells_raw = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    cells = [[int(value) for value in cell] for cell in cells_raw]
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    return {"stepId": step_id, "points": points, "cells": cells, "materialIds": material_ids, "materialMap": material_map(domain)}


def apply_step(domain, project, step):
    model = step["model"]
    if model == "removeMaterial":
        domain.removeMaterial(material(step["material"]))
    elif model == "planarize":
        ps.Planarize(domain, float(step["cutoffHeight"])).apply()
    elif model == "boolean":
        factory = ps.GeometryFactory(domain.getSetup(), f"GeometryReferenceBoolean_{step['id']}")
        position = [float(value) for value in step["position"]]
        if project["dimension"] == 2:
            operand = factory.makeBoxStencil(position[:2], float(step["width"]), float(step["height"]), float(step.get("angle", 0.0)))
        else:
            operand = factory.makeBoxStencil(position, float(step["width"]), float(step["height"]), float(step.get("angle", 0.0)), float(step["depth"]))
        enum_name = {"union": "UNION", "intersection": "INTERSECT", "difference": "RELATIVE_COMPLEMENT"}[step["booleanOperation"]]
        domain.applyBooleanOperation(operand, getattr(ps.ls.BooleanOperationEnum, enum_name), bool(step.get("applyToAll", True)))
    elif model == "isotropic":
        if step["operation"] == "deposit":
            domain.duplicateTopLevelSet(material(step["depositMaterial"]))
        rate = float(step["rateMagnitude"]) * (-1.0 if step["operation"] == "etch" else 1.0)
        process = ps.Process(domain, ps.IsotropicProcess(rate=rate, maskMaterials=[]), float(step["duration"]))
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        parameters = ps.AdvectionParameters()
        parameters.timeStepRatio = float(project["solver"]["timeStepRatio"])
        process.setParameters(parameters)
        process.apply()
    else:
        raise ValueError(f"unsupported reference step {model!r}")


def main():
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    project = json.loads(source.read_text(encoding="utf-8"))
    domain = build_domain(project)
    result = {"initial": snapshot(domain, "initial", project["dimension"])}
    for step in project["steps"]:
        if not step["enabled"]:
            continue
        apply_step(domain, project, step)
        result[step["id"]] = snapshot(domain, step["id"], project["dimension"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dimension": project["dimension"], "stepIds": list(result)}))


if __name__ == "__main__":
    main()
