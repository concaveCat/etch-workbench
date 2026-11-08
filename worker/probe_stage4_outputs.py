"""Directly probe flux and velocity outputs from the installed ViennaPS binding."""
from __future__ import annotations

import argparse
import json
from importlib.metadata import version
from pathlib import Path


def mesh_report(mesh):
    def data(item):
        scalars = {}
        vectors = {}
        for index in range(item.getScalarDataSize()):
            scalars[str(item.getScalarDataLabel(index))] = [float(x) for x in item.getScalarData(index)]
        for index in range(item.getVectorDataSize()):
            vectors[str(item.getVectorDataLabel(index))] = [[float(x) for x in row] for row in item.getVectorData(index)]
        return {"scalars": scalars, "vectors": vectors}

    return {
        "points": [[float(x) for x in row] for row in mesh.getNodes()],
        "lines": [[int(x) for x in row] for row in mesh.getLines()],
        "pointData": data(mesh.getPointData()),
        "cellData": data(mesh.getCellData()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    import viennaps as ps

    ps.setDimension(2)
    ps.setNumThreads(1)
    domain = ps.Domain(0.1, 4.0)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()

    model = ps.SingleParticleProcess(rate=0.2, stickingProbability=0.5, sourceExponent=1.0, maskMaterials=[])
    process = ps.Process(domain, model, 0.1)
    process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = 64
    rays.rngSeed = 20261026
    rays.useRandomSeeds = False
    process.setParameters(rays)
    flux = process.calculateFlux()
    flux_report = mesh_report(flux)

    advection = ps.AdvectionParameters()
    advection.velocityOutput = True
    advection.calculateIntermediateVelocities = True
    process.setParameters(advection)
    intermediate = args.output / "intermediate"
    intermediate.mkdir(exist_ok=True)
    process.setIntermediateOutputPath(str(intermediate))
    process.apply()
    surface_report = mesh_report(domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01))

    report = {
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "advectionMetadata": advection.toMetaData(),
        "rayMetadata": rays.toMetaData(),
        "fluxMesh": flux_report,
        "postProcessSurfaceMesh": surface_report,
        "intermediateFiles": sorted({path.name for path in intermediate.rglob("*") if path.is_file()}),
    }
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
