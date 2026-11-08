"""Prove the multi-material GDS nesting rule with native APIs only.

This script deliberately does not import Workbench code.  It builds two GDS
layers on the native GDS geometry grid, inserts them with or without the
cumulative union and reports each material's extent after one isotropic step.
The ``wrap=False`` runs must show the lower material clipped to the last
layer's footprint; the ``wrap=True`` runs must keep it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def stats(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True)
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    points = mesh.getNodes()
    data = mesh.getCellData()
    ids = next(list(data.getScalarData(i)) for i in range(data.getScalarDataSize())
               if data.getScalarDataLabel(i) == "MaterialIds")
    by_material = {}
    for material in sorted(set(map(int, ids))):
        vertices = {vertex for cell, mid in zip(cells, ids) if int(mid) == material for vertex in cell}
        by_material[str(material)] = {
            "cells": sum(int(mid) == material for mid in ids),
            "bounds": [fn(points[index][axis] for index in vertices)
                       for axis in range(dimension) for fn in (min, max)]}
    return {"points": len(points), "materials": by_material,
            "levelSetPoints": [level.getNumberOfPoints() for level in domain.getLevelSets()]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("dimension", choices=["2", "3"])
    parser.add_argument("gds", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--wrap", choices=["false", "true"], required=True)
    parser.add_argument("--delta", type=float, default=0.2)
    parser.add_argument("--duration", type=float, default=0.2)
    args = parser.parse_args()
    dimension = int(args.dimension)
    wrap = args.wrap == "true"

    import viennaps as ps

    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    reflective = ps.BoundaryType.REFLECTIVE_BOUNDARY
    if dimension == 2:
        domain = ps.Domain(args.delta, 6.0, reflective)
        geometry = ps.d2.GDSGeometry(args.delta)
        geometry.setBoundaryConditions([reflective, ps.BoundaryType.INFINITE_BOUNDARY])
        ps.d2.GDSReader(geometry, str(args.gds)).apply()
        layers = [(geometry.layerToLevelSet(1, True), ps.Material.Si),
                  (geometry.layerToLevelSet(2, True), ps.Material.SiO2)]
    else:
        domain = ps.Domain(args.delta, 6.0, 4.0, reflective)
        geometry = ps.d3.GDSGeometry(args.delta)
        geometry.setBoundaryConditions(
            [reflective, reflective, ps.BoundaryType.INFINITE_BOUNDARY])
        ps.d3.GDSReader(geometry, str(args.gds)).apply()
        # base/height chosen so the installed 3D layerToLevelSet is non-empty.
        layers = [(geometry.layerToLevelSet(1, 0.0, 0.6, False, True), ps.Material.Si),
                  (geometry.layerToLevelSet(2, 0.4, 0.4, False, True), ps.Material.SiO2)]
    report = {"args": {key: (str(value) if isinstance(value, Path) else value)
                       for key, value in vars(args).items()}}
    for level_set, material in layers:
        domain.insertNextLevelSetAsMaterial(level_set, material, wrap)
    report["before"] = stats(domain, dimension)
    process = ps.Process(domain, ps.IsotropicProcess(rate=-0.2, maskMaterials=[]), args.duration)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    process.apply()
    report["after"] = stats(domain, dimension)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"dimension": dimension, "wrap": wrap,
                      "after": report["after"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
