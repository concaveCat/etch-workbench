"""Independent installed-ViennaPS reference for a native-state continuation.

This fixture must run through scripts/bounded_child.py. It writes and reads the
native .vpsd itself, then compares the resumed process with an uninterrupted
direct call on the same installed ViennaPS build.
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


def make_domain(project):
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    settings = project["domain"]
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if settings.get("xBoundary", settings.get("boundaryXY")) == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if project["dimension"] == 2:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), float(settings["yExtent"]), boundary)
    geometry = project["geometry"]
    ps.MakePlane(domain, height=float(geometry["height"]), material=material(geometry["substrateMaterial"]), addToExisting=False).apply()
    return domain


def apply(domain, project, step):
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(material(step["depositMaterial"]))
        velocity = float(step["rateMagnitude"])
    else:
        velocity = -float(step["rateMagnitude"])
    process = ps.Process(domain, ps.IsotropicProcess(rate=velocity, maskMaterials=[]), float(step["duration"]))
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    parameters = ps.AdvectionParameters()
    parameters.timeStepRatio = float(project["solver"]["timeStepRatio"])
    process.setParameters(parameters)
    process.apply()


def snapshot(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = [[float(value) for value in point] for point in mesh.getNodes()]
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    cells = [[int(value) for value in cell] for cell in cells]
    data = mesh.getCellData()
    material_ids = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    material_map = [int(mapping.getMaterialIdAtIdx(index)) for index in range(mapping.size())]
    return {"points": points, "cells": cells, "materialIds": material_ids, "materialMap": material_map,
            "gridDelta": float(domain.getGridDelta()), "boundaryConditions": [str(value) for value in domain.getBoundaryConditions()],
            "levelSetCount": int(domain.getNumberOfLevelSets())}


def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    state_path = Path(sys.argv[3])
    project = json.loads(source.read_text(encoding="utf-8"))
    first, second = project["steps"]

    direct = make_domain(project)
    apply(direct, project, first)
    ps.Writer(direct, str(state_path)).apply()
    if not state_path.is_file() or state_path.stat().st_size == 0:
        raise RuntimeError("ViennaPS Writer did not produce a continuation state")

    resumed = make_domain(project)
    ps.Reader(resumed, str(state_path)).apply()
    after_read = snapshot(resumed, project["dimension"])
    apply(resumed, project, second)
    resumed_output = snapshot(resumed, project["dimension"])

    uninterrupted = make_domain(project)
    apply(uninterrupted, project, first)
    apply(uninterrupted, project, second)
    uninterrupted_output = snapshot(uninterrupted, project["dimension"])
    if resumed_output != uninterrupted_output:
        raise RuntimeError("ViennaPS Writer/Reader continuation differs from uninterrupted direct ViennaPS output")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"versions": {"viennaps": ps.version, "viennals": ps.ls.version},
                                  "afterRead": after_read, "resumed": resumed_output,
                                  "uninterrupted": uninterrupted_output,
                                  "nativeStateBytes": state_path.stat().st_size}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dimension": project["dimension"], "resumedMatchesUninterrupted": True,
                      "nativeStateBytes": state_path.stat().st_size}))


if __name__ == "__main__":
    main()
