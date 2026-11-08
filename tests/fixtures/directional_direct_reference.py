"""Independent ViennaPS reference for DirectionalProcess API comparisons.

This file is always run through scripts/bounded_child.py by its parent test.
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
        ps.MakePlane(domain, height=float(geometry["height"]), material=material(geometry["substrateMaterial"]), addToExisting=False).apply()
    elif geometry["kind"] == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=float(geometry["width"]), trenchDepth=float(geometry["depth"]),
                      trenchTaperAngle=float(geometry["taperAngle"]), maskHeight=float(geometry["maskHeight"]),
                      maskTaperAngle=float(geometry["maskTaperAngle"]), halfTrench=False,
                      material=material(geometry["substrateMaterial"]), maskMaterial=material(geometry.get("maskMaterial", "Mask"))).apply()
    else:
        raise ValueError("reference fixture supports plane and trench inputs")
    return domain


def mask_material_names(project):
    geometry = project["geometry"]
    if geometry["kind"] == "trench" and geometry["maskHeight"] > 0:
        return [geometry.get("maskMaterial", "Mask")]
    if geometry["kind"] == "stack":
        return list(dict.fromkeys(layer["material"] for layer in geometry["layers"] if layer["role"] == "mask"))
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
    return {"stepId": step_id, "points": points, "cells": cells, "materialIds": material_ids, "materialMap": material_map}


def run_step(domain, project, step):
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(material(step["depositMaterial"]))
    sign = -1.0 if step["operation"] == "etch" else 1.0
    default_masks = [material(name) for name in mask_material_names(project)]
    if step.get("rateSets") is not None:
        rate_sets = []
        for item in step["rateSets"]:
            direction = [float(value) for value in item["direction"]]
            norm = math.sqrt(sum(value * value for value in direction))
            if norm <= 1e-12:
                raise RuntimeError("invalid zero reference direction")
            direction = [value / norm for value in direction]
            masks = (default_masks if item.get("maskMaterials") is None
                     else [material(name) for name in item["maskMaterials"]])
            directional = float(item["directionalVelocity"])
            rate_sets.append(ps.RateSet(
                direction, directional * sign, float(item.get("isotropicVelocity", 0.0)) * sign,
                masks, bool(item.get("calculateVisibility", True)) and directional > 0))
        model = ps.DirectionalProcess(rate_sets)
    else:
        direction = [float(value) for value in step["direction"]]
        norm = math.sqrt(sum(value * value for value in direction))
        if norm <= 1e-12:
            raise RuntimeError("invalid zero reference direction")
        direction = [value / norm for value in direction]
        directional_velocity = float(step["directionalRateMagnitude"]) * sign
        isotropic_velocity = float(step["isotropicRateMagnitude"]) * sign
        visibility = bool(step["calculateVisibility"]) if step["directionalRateMagnitude"] > 0 else False
        model = ps.DirectionalProcess(direction, directional_velocity, isotropic_velocity,
                                      default_masks, visibility)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(advection)
    process.apply()


def has_motion(step):
    if step.get("rateSets") is not None:
        return any(float(item["directionalVelocity"]) > 0 or float(item["isotropicVelocity"]) > 0
                   for item in step["rateSets"])
    return step["directionalRateMagnitude"] != 0 or step["isotropicRateMagnitude"] != 0


def main():
    source = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    project = json.loads(source.read_text(encoding="utf-8"))
    domain = build_domain(project)
    output = {"initial": snapshot(domain, "initial")}
    for step in project["steps"]:
        if not step["enabled"]:
            continue
        if not has_motion(step):
            output[step["id"]] = snapshot(domain, step["id"])
            continue
        run_step(domain, project, step)
        output[step["id"]] = snapshot(domain, step["id"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stepIds": list(output), "dimension": project["dimension"]}))


if __name__ == "__main__":
    main()
