"""Direct installed-binding smoke run for one Stage 8 native model.

Every invocation is launched by scripts/bounded_child.py. It records the actual
ViennaPS mesh before and after the native Process.apply() call.
"""
from __future__ import annotations

import importlib.metadata
import json
import sys
import time
from pathlib import Path

import viennaps as ps


def snapshot(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    data = mesh.getCellData()
    materials = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            materials = [int(value) for value in data.getScalarData(index)]
    if materials is None:
        raise RuntimeError("native result mesh has no MaterialIds cell array")
    mapping = domain.getMaterialMap()
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    def arrays(data):
        scalars = {str(data.getScalarDataLabel(i)): [float(value) for value in data.getScalarData(i)]
                   for i in range(data.getScalarDataSize())}
        vectors = {str(data.getVectorDataLabel(i)): [[float(value) for value in row] for row in data.getVectorData(i)]
                   for i in range(data.getVectorDataSize())}
        return {"scalars": scalars, "vectors": vectors}
    try:
        metadata = {str(key): [float(value) for value in values] for key, values in domain.getMetaData().items()}
    except Exception as exc:
        metadata = {"readError": f"{type(exc).__name__}: {exc}"}
    cell_set = None
    try:
        native_cells = domain.getCellSet()
        if native_cells is not None:
            cell_set = {"numberOfCells": int(native_cells.getNumberOfCells()),
                        "fields": {str(label): [float(value) for value in native_cells.getScalarData(str(label))]
                                   for label in native_cells.getScalarDataLabels()}}
    except Exception as exc:
        cell_set = {"readError": f"{type(exc).__name__}: {exc}"}
    return {
        "points": [[float(value) for value in row] for row in mesh.getNodes()],
        "cells": [[int(value) for value in row] for row in cells],
        "materialIds": materials,
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "pointData": arrays(mesh.getPointData()),
        "cellData": arrays(mesh.getCellData()),
        "nativeMetadata": metadata,
        "cellSet": cell_set,
    }


def make_domain(dimension, name):
    ps.setDimension(dimension)
    ps.setNumThreads(1)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    delta, extent = 0.2, 4.0
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY
    domain = ps.Domain(delta, extent, boundary=boundary) if dimension == 2 else ps.Domain(delta, extent, extent, boundary)
    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    if name == "selectiveEpitaxy":
        return domain
    ps.MakePlane(domain, height=0.4, material=ps.Material.SiO2, addToExisting=True).apply()
    ps.MakePlane(domain, height=0.6, material=ps.Material.Si3N4, addToExisting=True).apply()
    factory = ps.GeometryFactory(domain.getSetup(), "Stage8Smoke")
    position = [0.0] * dimension
    position[-1] = 0.401
    if dimension == 2:
        stencil = factory.makeBoxStencil(position, 1.2, 0.202, 0.0)
    else:
        stencil = factory.makeBoxStencil(position, 1.2, 0.202, 0.0, 3.6)
    domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT, False)
    return domain


def make_model(name):
    if name == "sf6o2":
        return ps.SF6O2Etching(1.0, 1.0, 0.2, 100.0, 10.0, 100.0, 3.0)
    if name == "cf4o2":
        return ps.CF4O2Etching(1.0, 1.0, 0.2, 0.2, 100.0, 10.0, 100.0, 3.0, 3.0)
    if name == "hbro2":
        return ps.HBrO2Etching(1.0, 1.0, 0.2, 100.0, 10.0, 100.0, 3.0)
    if name == "sf6c4f8":
        return ps.SF6C4F8Etching(1.0, 1.0, 100.0, 10.0, 300.0)
    if name == "fluorocarbon":
        parameters = ps.FluorocarbonParameters()
        parameters.ionFlux = 1.0
        parameters.etchantFlux = 1.0
        parameters.polyFlux = 0.2
        for material in (ps.Material.Si, ps.Material.SiO2, ps.Material.Si3N4, ps.Material.Mask, ps.Material.Polymer):
            material_parameters = ps.FluorocarbonMaterialParameters()
            material_parameters.id = material
            parameters.addMaterial(material_parameters)
        return ps.FluorocarbonEtching(parameters)
    if name == "ionBeam":
        parameters = ps.IBEParameters()
        parameters.meanEnergy = 100.0
        parameters.planeWaferRate = 0.1
        parameters.redepositionRate = 0.0
        return ps.IonBeamEtching(parameters, [ps.Material.Mask])
    if name == "wetEtch":
        return ps.WetEtching([(ps.Material.Si, 0.1), (ps.Material.SiO2, 0.02), (ps.Material.Si3N4, 0.0)])
    if name == "teosDeposition":
        return ps.TEOSDeposition(0.5, 0.05, 1.0)
    if name == "teospecvd":
        return ps.TEOSPECVD(0.5, 0.05, 0.02, 1.0)
    if name == "ald":
        parameters = ps.SingleParticleALDParams()
        parameters.stickingProbability = 0.5
        parameters.gasMeanFreePath = 1.0
        parameters.growthPerCycle = 0.02
        parameters.incomingFlux = 1.0
        parameters.evaporationFlux = 0.1
        parameters.s0 = 1.0
        model = ps.SingleParticleALD(parameters)
        return model
    if name == "oxidation":
        model = ps.Oxidation()
        oxidant = getattr(ps.OxidantType, "Dry", None)
        if oxidant is None:
            oxidant = getattr(ps.OxidantType, "DRY")
        model.setOxidant(oxidant)
        model.setOrientation(ps.SiliconOrientation.Si100)
        model.setTemperature(1000.0)
        model.setPressure(1.0)
        model.setTime(0.001)
        model.setTimeStep(0.0001)
        return model
    if name == "oxideRegrowth":
        return ps.OxideRegrowth(0.01, 0.005, 0.001, 0.01, 0.1, 0.02, 0.1, 0.05, 0.05, 0.6, 1.2, 0.15)
    if name == "selectiveEpitaxy":
        return ps.SelectiveEpitaxy([(ps.Material.Si, 0.05), (ps.Material.SiGe, 0.05)], 0.02, 0.05)
    if name == "neutralTransport":
        parameters = ps.NeutralTransportParameters()
        parameters.incomingFlux = 0.2
        parameters.sourceDistributionPower = 1.0
        parameters.etchFrontSticking = 0.5
        parameters.zeroCoverageSticking = 0.1
        parameters.coverageTimeStep = 0.05
        parameters.useSteadyStateCoverage = True
        model = ps.NeutralTransport(parameters)
        return model
    raise ValueError(f"unknown model smoke name {name!r}")


def main(name, dimension, output):
    row = {"model": name, "dimension": dimension, "versions": {
        "viennaps": importlib.metadata.version("viennaps"), "viennals": importlib.metadata.version("viennals")}}
    try:
        domain = make_domain(dimension, name)
        row["initial"] = snapshot(domain, dimension)
        if name in {"teosDeposition", "teospecvd", "ald", "selectiveEpitaxy"}:
            deposit_material = ps.Material.Al2O3 if name == "ald" else ps.Material.SiGe if name == "selectiveEpitaxy" else ps.Material.SiO2
            domain.duplicateTopLevelSet(deposit_material)
        if name == "oxideRegrowth":
            domain.generateCellSet(10.6, ps.Material.GAS, True)
            cell_set = domain.getCellSet()
            cell_set.addScalarData("byproductSum", 0.0)
            cell_set.buildNeighborhood()
        model = make_model(name)
        row["nativeClass"] = f"{type(model).__module__}.{type(model).__name__}"
        process = ps.Process(domain, model, 0.02)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        advection = ps.AdvectionParameters()
        advection.timeStepRatio = 0.25
        if name == "oxideRegrowth":
            advection.ignoreVoids = True
        process.setParameters(advection)
        if name in {"sf6o2", "cf4o2", "hbro2", "sf6c4f8"}:
            coverage = ps.CoverageParameters()
            coverage.tolerance = 1e-3
            process.setParameters(coverage)
        if name == "ald":
            rays = ps.RayTracingParameters()
            rays.raysPerPoint = 32
            rays.rngSeed = 271828
            process.setParameters(rays)
            atomic = ps.AtomicLayerProcessParameters()
            atomic.pulseTime = 0.02
            atomic.coverageTimeStep = 0.01
            atomic.numCycles = 1
            process.setParameters(atomic)
        started = time.monotonic()
        process.apply()
        row["elapsedSeconds"] = time.monotonic() - started
        row["final"] = snapshot(domain, dimension)
        row["status"] = "succeeded"
    except Exception as exc:
        row["status"] = "failed"
        row["error"] = f"{type(exc).__name__}: {exc}"
    Path(output).write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: stage8_model_smoke.py MODEL DIMENSION OUTPUT.json")
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3])
