"""Small real 2D/3D MultiParticleProcess probe for the installed ViennaPS."""
from __future__ import annotations

import importlib.metadata
import json
import sys
import time
from collections import Counter
from pathlib import Path

import viennaps as ps


def snapshot(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=.01)
    data = mesh.getCellData()
    material_ids = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    return {
        "points": [[float(value) for value in row] for row in mesh.getNodes()],
        "cells": mesh.getTriangles() if dimension == 3 else mesh.getLines(),
        "materialIds": material_ids,
        "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i))
                                       for i in range(domain.getMaterialMap().size())],
    }


def run_case(dimension):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    binding = getattr(ps, f"d{dimension}")
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = (binding.Domain(.2, 2.4, 2.4, boundary) if dimension == 3
              else binding.Domain(.2, 2.4, boundary))
    ps.MakeStack(domain, numLayers=2, layerHeight=.4, substrateHeight=.4, trenchWidth=.8).apply()

    model = binding.MultiParticleProcess()
    model.addIonParticle(sourcePower=1.0, thetaRMin=0.0, thetaRMax=50.0, minAngle=60.0,
                         B_sp=-1.0, meanEnergy=120.0, sigmaEnergy=15.0, thresholdEnergy=10.0,
                         inflectAngle=25.0, n=1.0, label="argonIon")
    model.addNeutralParticle({ps.Material.Si: .45, ps.Material.SiO2: .6, ps.Material.Si3N4: .25},
                             defaultStickingProbability=.4, label="fluorineNeutral")
    callback_samples = []
    callback_material_counts = Counter()

    def rate_function(fluxes, material):
        values = [float(value) for value in fluxes]
        material_name = str(material)
        callback_material_counts[material_name] += 1
        if len(callback_samples) < 32:
            callback_samples.append({"fluxVector": values, "material": material_name})
        # Return a small signed velocity from the native species-flux vector.
        return -0.0001 * sum(values)

    model.setRateFunction(rate_function)
    process = binding.Process(domain, model, .05)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    ray = ps.RayTracingParameters()
    ray.raysPerPoint = 64
    ray.rngSeed = 271828
    ray.useRandomSeeds = False
    ray.maxReflections = 3
    ray.maxBoundaryHits = 80
    process.setParameters(ray)
    flux_mesh = process.calculateFlux()
    flux_cells = flux_mesh.getCellData()
    flux_field_names = [str(flux_cells.getScalarDataLabel(i)) for i in range(flux_cells.getScalarDataSize())]
    flux_field_samples = {
        name: [float(value) for value in flux_cells.getScalarData(index)[:5]]
        for index, name in enumerate(flux_field_names)
    }
    started = time.monotonic()
    process.apply()
    return {
        "dimension": dimension,
        "engine": "CPU_DISK",
        "seed": ray.rngSeed,
        "threads": 1,
        "species": ["argonIon", "fluorineNeutral"],
        "callbackSampleCountCapped": len(callback_samples),
        "callbackSamples": callback_samples,
        "callbackMaterialInvocationCounts": dict(callback_material_counts),
        "fluxCellFieldNames": flux_field_names,
        "fluxCellFieldSamples": flux_field_samples,
        "rayMetadata": ray.toMetaData(),
        "mesh": snapshot(domain, dimension),
        "elapsedSeconds": time.monotonic() - started,
    }


def main(path):
    result = {"versions": {"viennaps": importlib.metadata.version("viennaps"),
                           "viennals": importlib.metadata.version("viennals")},
              "cases": [run_case(2), run_case(3)]}
    Path(path).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"versions": result["versions"],
                      "cases": [{"dimension": row["dimension"], "points": len(row["mesh"]["points"]),
                                 "callbackSamples": len(row["callbackSamples"]),
                                 "fluxCellFieldNames": row["fluxCellFieldNames"],
                                 "fluxArity": sorted({len(item["fluxVector"]) for item in row["callbackSamples"]})}
                                for row in result["cases"]]}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
