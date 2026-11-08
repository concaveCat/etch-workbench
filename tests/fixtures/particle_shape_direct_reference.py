"""Independent ViennaPS reference for schema-7 ``particleShape`` steps.

Always run this fixture inside scripts/bounded_child.py. It never imports
workbench worker code: it builds the domain from the submitted project, applies
the native ``BoxDistribution`` / ``SphereDistribution`` to every enabled
``particleShape`` step in order and records one surface snapshot per step.

``argv[3]`` selects the mask contract: ``native`` (default) re-derives the
geometry mask materials and calls ``addMaskMaterial`` for masked etches, while
``none`` runs the no-mask overload. Tests use both to prove the mask changes
the geometry instead of asserting it in prose.
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
    delta = float(project["domain"]["gridDelta"])
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if project["domain"].get(
        "xBoundary", project["domain"].get("boundaryXY")) == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if project["dimension"] == 2:
        domain = ps.Domain(delta, float(project["domain"]["xExtent"]), boundary)
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


def geometry_mask_names(project, step):
    """Re-derive the project mask contract independently of the worker.

    A geometric etch protects the geometry's native mask material(s); a
    deposition cannot be masked natively in 4.6.2/4.7.0, so it never gets one.
    """
    if step["operation"] != "etch":
        return []
    geometry = project["geometry"]
    if geometry["kind"] in ("trench", "hole", "fin") and float(geometry.get("maskHeight", 0)) > 0:
        return [geometry.get("maskMaterial", "Mask")]
    return []


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
    return {"stepId": step_id, "points": points, "cells": cells,
            "materialIds": material_ids, "materialMap": material_map(domain)}


def apply_step(domain, project, step, mask_mode="native"):
    dimension = int(project["dimension"])
    sign = -1.0 if step["operation"] == "etch" else 1.0
    if step["shape"] == "box":
        axes = [sign * float(value) for value in step["halfAxes"]]
        model = ps.BoxDistribution(axes + [0.0] if dimension == 2 else axes)
    else:
        model = ps.SphereDistribution(sign * float(step["radius"]))
    if mask_mode == "native":
        for name in geometry_mask_names(project, step):
            level_set = domain.getMaterialLevelSet(material(name))
            if level_set is not None and int(level_set.getNumberOfPoints()) > 0:
                model.addMaskMaterial(material(name))
    process = ps.Process(domain, model, 0.0)
    # Mirror the worker: the Process default is AUTO and initialises CUDA even
    # for a geometric distribution, so the reference pins the CPU backend.
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()


def main() -> None:
    project = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    target = Path(sys.argv[2])
    mask_mode = sys.argv[3] if len(sys.argv) > 3 else "native"
    if mask_mode not in ("native", "none"):
        raise ValueError("mask mode must be 'native' or 'none'")
    dimension = int(project["dimension"])
    domain = build_domain(project)
    reference = {"initial": snapshot(domain, "initial", dimension)}
    for step in project["steps"]:
        if not step.get("enabled"):
            continue
        if step.get("model") != "particleShape":
            raise ValueError(f"reference fixture only supports particleShape steps, got {step.get('model')!r}")
        apply_step(domain, project, step, mask_mode)
        reference[step["id"]] = snapshot(domain, step["id"], dimension)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(reference, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"steps": sorted(key for key in reference if key != "initial")}))


if __name__ == "__main__":
    main()
