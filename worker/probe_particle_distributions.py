"""Isolated probe for ViennaPS 4.6.2 particle-shape distributions.

Run one case per process through ``scripts/bounded_child.py``: a masked
deposition and a ``CustomSphereDistribution`` abort the native library, so the
parent must survive to record the guard file. The probe never imports workbench
worker code, never reads a project and never contacts the API.

Usage::

    python worker/probe_particle_distributions.py <case.json> <output.json>

``case.json`` keys::

    dimension   2 or 3
    shape       "box" | "sphere" | "customSphere"
    magnitude   number; negative values ask the distribution to etch
    halfAxes    optional [x, y, z] for shape=box (defaults to magnitude)
    radii       optional list for shape=customSphere
    mask        "none" | "material" | "raw-material" | "surface"
    geometry    "plane" | "trench" | "stack"
    duration    process duration; native geometric advection ignores it
    boundary    "reflective" | "periodic"
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import viennaps as ps


def material(name: str):
    registry = ps.MaterialRegistry.instance()
    found = registry.findMaterial(name)
    if found is None:
        raise RuntimeError(f"material {name!r} is not registered")
    return found


def build_domain(dimension: int, geometry: str, boundary: str):
    boundary_type = (ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary == "reflective"
                     else ps.BoundaryType.PERIODIC_BOUNDARY)
    if dimension == 2:
        domain = ps.Domain(0.05, 2.0, boundary_type)
    else:
        domain = ps.Domain(0.05, 2.0, 2.0, boundary_type)
    ps.MakePlane(domain, height=0.0, material=material("Si"), addToExisting=False).apply()
    if geometry == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=0.6, trenchDepth=0.4, trenchTaperAngle=0.0,
                      maskHeight=0.2, maskTaperAngle=0.0, halfTrench=False,
                      material=material("Si"), maskMaterial=material("Mask")).apply()
    elif geometry == "stack":
        ps.MakePlane(domain, height=0.3, material=material("SiO2"), addToExisting=True).apply()
        ps.MakePlane(domain, height=0.5, material=material("Si3N4"), addToExisting=True).apply()
    return domain


def summary(domain, dimension: int) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    points = mesh.getNodes()
    cell_data = mesh.getCellData()
    material_ids: list[int] = []
    for index in range(cell_data.getScalarDataSize()):
        if str(cell_data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in cell_data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    return {
        "boundingBox": [[round(float(min(point[axis] for point in points)), 6),
                         round(float(max(point[axis] for point in points)), 6)]
                        for axis in range(dimension)],
        "surfaceMaterials": sorted(set(material_ids)),
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "levelSetPointCounts": [int(level_set.getNumberOfPoints()) for level_set in domain.getLevelSets()],
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
        "cellCount": len(mesh.getLines() if dimension == 2 else mesh.getTriangles()),
    }


def distribution(case: dict, domain):
    dimension = int(case["dimension"])
    shape = str(case["shape"])
    mask_mode = str(case.get("mask", "none"))
    magnitude = float(case.get("magnitude", 0.0))
    if mask_mode == "raw-material":
        # A raw viennals Domain taken from the project's own material level set.
        mask = ps.ls.Domain(domain.getMaterialLevelSet(material("Mask")))
        magnitude = float(case.get("magnitude", 0.0))
        if shape == "box":
            return ps.BoxDistribution([float(value) for value in case.get("halfAxes", [magnitude] * 3)], mask)
        if shape == "sphere":
            return ps.SphereDistribution(magnitude, mask)
        return ps.CustomSphereDistribution([float(value) for value in case["radii"]], mask)
    if shape == "box":
        model = ps.BoxDistribution([float(value) for value in case.get("halfAxes", [magnitude] * 3)])
    elif shape == "sphere":
        model = ps.SphereDistribution(magnitude)
    elif shape == "customSphere":
        model = ps.CustomSphereDistribution([float(value) for value in case["radii"]])
    else:
        raise ValueError(f"unknown shape {shape!r}")
    if mask_mode == "material":
        model.addMaskMaterial(material("Mask"))
    elif mask_mode == "surface":
        model.applyToSingleMaterial(material("Si"))
    return model


def main() -> None:
    case = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    target = Path(sys.argv[2])
    dimension = int(case["dimension"])
    ps.setDimension(dimension)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setNumThreads(1)
    domain = build_domain(dimension, str(case.get("geometry", "plane")),
                          str(case.get("boundary", "reflective")))
    before = summary(domain, dimension)
    started = time.monotonic()
    model = distribution(case, domain)
    process = ps.Process(domain, model, float(case.get("duration", 0.0)))
    # AUTO resolves a flux engine and initialises CUDA even for a geometric
    # distribution; mirror the worker's explicit CPU backend.
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    elapsed = time.monotonic() - started
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "versions": {"viennaps": ps.version, "viennals": ps.ls.version},
        "case": case,
        "nativeClass": type(model).__name__,
        "before": before,
        "after": summary(domain, dimension),
        "elapsedSeconds": round(elapsed, 4),
    }, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "nativeClass": type(model).__name__, "elapsedSeconds": round(elapsed, 4)}))


if __name__ == "__main__":
    main()
