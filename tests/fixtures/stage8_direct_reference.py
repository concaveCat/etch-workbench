"""Independent direct ViennaPS 4.6.2 reference for a schema-7 specialized step.

This fixture deliberately does not import the product worker's model builder.
Invoke it only through scripts/bounded_child.py.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

import viennaps as ps


def material(name: str):
    return getattr(ps.Material, name)


def arrays(data: Any) -> dict[str, Any]:
    return {
        "scalars": {str(data.getScalarDataLabel(i)): [float(v) for v in data.getScalarData(i)] for i in range(data.getScalarDataSize())},
        "vectors": {str(data.getVectorDataLabel(i)): [[float(v) for v in row] for row in data.getVectorData(i)] for i in range(data.getVectorDataSize())},
    }


def snapshot(domain: Any, dimension: int) -> dict[str, Any]:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    cell_data = arrays(mesh.getCellData())
    scalar_arrays = cell_data["scalars"]
    if "MaterialIds" not in scalar_arrays:
        raise RuntimeError("direct ViennaPS surface output has no MaterialIds")
    vertical = 1 if dimension == 2 else 2
    cell_set = None
    try:
        item = domain.getCellSet()
        if item is not None:
            cell_set = {"numberOfCells": int(item.getNumberOfCells()),
                        "fields": {str(label): [float(value) for value in item.getScalarData(str(label))]
                                   for label in item.getScalarDataLabels()}}
    except Exception as exc:
        cell_set = {"readError": f"{type(exc).__name__}: {exc}"}
    try:
        metadata = {str(key): [float(value) for value in values] for key, values in domain.getMetaData().items()}
    except Exception as exc:
        metadata = {"readError": f"{type(exc).__name__}: {exc}"}
    return {
        "points": [[float(v) for v in row] for row in mesh.getNodes()],
        "cells": [[int(v) for v in row] for row in (mesh.getLines() if dimension == 2 else mesh.getTriangles())],
        "materialIds": [int(v) for v in scalar_arrays["MaterialIds"]],
        "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i)) for i in range(domain.getMaterialMap().size())],
        "levelSetCount": int(domain.getNumberOfLevelSets()),
        "pointData": arrays(mesh.getPointData()), "cellData": cell_data,
        "nativeMetadata": metadata, "cellSet": cell_set,
        "verticalAxis": vertical,
    }


def make_domain(project: dict[str, Any]):
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    d = project["domain"]
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if (d.get("xBoundary", d.get("boundaryXY")) == "reflective") else ps.BoundaryType.PERIODIC_BOUNDARY
    domain = ps.Domain(float(d["gridDelta"]), float(d["xExtent"]), boundary=boundary) if dimension == 2 else ps.Domain(float(d["gridDelta"]), float(d["xExtent"]), float(d["yExtent"]), boundary)
    geometry = project["geometry"]
    ps.MakePlane(domain, height=float(geometry["baseHeight"]), material=material(geometry["substrateMaterial"]), addToExisting=False).apply()
    height = float(geometry["baseHeight"])
    for layer in geometry["layers"]:
        height += float(layer["thickness"])
        ps.MakePlane(domain, height=height, material=material(layer["material"]), addToExisting=True).apply()
    pattern = geometry.get("pattern", {"kind": "none"})
    if pattern.get("kind") != "none":
        top_layer = geometry["layers"][-1]
        top_thickness = float(top_layer["thickness"])
        position = [0.0] * dimension
        position[-1] = height - top_thickness - float(d["gridDelta"]) * 1e-3
        angle = -float(pattern.get("taperAngle", 0.0))
        factory = ps.GeometryFactory(domain.getSetup(), "Stage8DirectStackPattern")
        if pattern["kind"] == "trench":
            stencil = factory.makeBoxStencil(position, float(pattern["width"]), top_thickness + 2 * float(d["gridDelta"]) * 1e-3, angle)
        else:
            stencil = factory.makeCylinderStencil(position, float(pattern["radius"]), top_thickness + 2 * float(d["gridDelta"]) * 1e-3, angle)
        domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT, False)
    if project["outputSettings"]["nativeMetadata"]:
        domain.enableMetaData(ps.MetaDataLevel.FULL)
    return domain


def make_model(step: dict[str, Any], domain: Any, project: dict[str, Any]):
    p = step["nativeParameters"]
    kind = step["nativeModel"]
    extra: list[Any] = []
    if kind == "sf6o2":
        model = ps.SF6O2Etching(p["ionFlux"], p["etchantFlux"], p["oxygenFlux"], p["meanIonEnergy"], p["sigmaIonEnergy"], p["ionExponent"], p["oxySputterYield"], p["etchStopDepth"])
        coverage = ps.CoverageParameters(); coverage.tolerance = 1e-3; extra.append(coverage)
    elif kind == "cf4o2":
        model = ps.CF4O2Etching(p["ionFlux"], p["etchantFlux"], p["oxygenFlux"], p["polymerFlux"], p["meanIonEnergy"], p["sigmaIonEnergy"], p["ionExponent"], p["oxySputterYield"], p["polySputterYield"], p["etchStopDepth"])
        coverage = ps.CoverageParameters(); coverage.tolerance = 1e-3; extra.append(coverage)
    elif kind == "hbro2":
        model = ps.HBrO2Etching(p["ionFlux"], p["etchantFlux"], p["oxygenFlux"], p["meanIonEnergy"], p["sigmaIonEnergy"], p["ionExponent"], p["oxySputterYield"], p["etchStopDepth"])
        coverage = ps.CoverageParameters(); coverage.tolerance = 1e-3; extra.append(coverage)
    elif kind == "sf6c4f8":
        model = ps.SF6C4F8Etching(p["ionFlux"], p["etchantFlux"], p["meanEnergy"], p["sigmaEnergy"], p["ionExponent"], p["etchStopDepth"])
        coverage = ps.CoverageParameters(); coverage.tolerance = 1e-3; extra.append(coverage)
    elif kind == "fluorocarbon":
        parameters = ps.FluorocarbonParameters()
        for key in ("ionFlux", "etchantFlux", "polyFlux", "k_ie", "k_ev", "delta_p", "temperature", "etchStopDepth"):
            setattr(parameters, key, float(p[key]))
        for key in ("meanEnergy", "sigmaEnergy", "exponent", "n_l", "inflectAngle", "minAngle"):
            setattr(parameters.Ions, key, float(p[key]))
        for name, values in sorted(p["materialParametersByMaterial"].items()):
            row = ps.FluorocarbonMaterialParameters(); row.id = material(name)
            for key, value in values.items(): setattr(row, key, float(value))
            parameters.addMaterial(row)
        model = ps.FluorocarbonEtching(parameters)
    elif kind == "ionBeam":
        parameters = ps.IBEParameters()
        for key in ("meanEnergy", "sigmaEnergy", "thresholdEnergy", "thetaRMin", "thetaRMax", "minAngle", "inflectAngle", "exponent", "n_l", "tiltAngle", "planeWaferRate", "redepositionRate", "redepositionThreshold"):
            setattr(parameters, key, float(p[key]))
        for index, key in enumerate(("cos4YieldA1", "cos4YieldA2", "cos4YieldA3", "cos4YieldA4"), start=1):
            setattr(parameters.cos4Yield, f"a{index}", float(p[key]))
        for name, value in sorted(p["materialPlaneWaferRateByMaterial"].items()): parameters.materialPlaneWaferRate.set(material(name), float(value))
        geometry = project["geometry"]
        if geometry.get("kind") == "gds":
            masks = [row["material"] for row in geometry.get("layers", []) if row.get("mask")]
        elif geometry.get("kind") == "stack":
            masks = [row["material"] for row in geometry.get("layers", []) if row.get("role") == "mask"]
        elif geometry.get("kind") in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0.0)) > 0:
            masks = [geometry.get("maskMaterial", "Mask")]
        else:
            masks = []
        model = ps.IonBeamEtching(parameters, [material(name) for name in masks])
    elif kind == "wetEtch":
        rows = [(material(name), float(value)) for name, value in sorted(p["materialRates"].items())]
        model = ps.WetEtching(p["direction100"], p["direction010"], p["rate100"], p["rate110"], p["rate111"], p["rate311"], rows) if p["useCrystallineRates"] else ps.WetEtching(rows)
    elif kind == "teosDeposition":
        model = ps.TEOSDeposition(p["stickingProbabilityP1"], p["rateP1"], p["orderP1"], p["stickingProbabilityP2"], p["rateP2"], p["orderP2"])
    elif kind == "teospecvd":
        model = ps.TEOSPECVD(p["stickingProbabilityRadical"], p["depositionRateRadical"], p["depositionRateIon"], p["exponentIon"], p["stickingProbabilityIon"], p["reactionOrderRadical"], p["reactionOrderIon"], p["minAngleIon"])
    elif kind == "ald":
        parameters = ps.SingleParticleALDParams()
        for key in ("stickingProbability", "gasMeanFreePath", "growthPerCycle", "incomingFlux", "evaporationFlux", "s0", "coverageDiffusionCoefficient"):
            setattr(parameters, key, float(p[key]))
        model = ps.SingleParticleALD(parameters)
        atomic = ps.AtomicLayerProcessParameters()
        for key in ("pulseTime", "purgePulseTime", "coverageTimeStep", "purgeTimeStep"): setattr(atomic, key, float(p[key]))
        atomic.numCycles = int(p["numCycles"]); extra.append(atomic)
        surface_diffusion = step.get("surfaceDiffusion")
        if surface_diffusion is not None and surface_diffusion.get("enabled", True):
            if float(p["coverageDiffusionCoefficient"]) <= 0.0:
                raise ValueError("surfaceDiffusion requires coverageDiffusionCoefficient > 0")
            native = ps.SurfaceDiffusionParameters()
            defaults = {key: getattr(native, key) for key in (
                "kNeighbors", "normalCutoff", "normalizeByLocalScale", "radius",
                "sigmaNormal", "stabilityFactor", "symmetrizeWeights")}
            for key, default in defaults.items():
                setattr(native, key, type(default)(surface_diffusion.get(key, default)))
            extra.append(native)
    elif kind == "oxidation":
        model = ps.Oxidation()
        oxidant = getattr(ps.OxidantType, p["oxidant"], None)
        if oxidant is None:
            oxidant = getattr(ps.OxidantType, str(p["oxidant"]).upper())
        model.setOxidant(oxidant); model.setOrientation(getattr(ps.SiliconOrientation, p["orientation"]))
        model.setTemperature(p["temperatureC"]); model.setPressure(p["pressureAtm"]); model.setTime(p["timeHours"]); model.setTimeStep(p["timeStepHours"])
        model.setTransferCoefficient(p["transferCoefficientUmHr"]); model.setInitialOxideThickness(p["initialOxideThicknessUm"])
        model.setReactionActivationVolume(p["reactionActivationVolumeM3"]); model.setDiffusionActivationVolume(p["diffusionActivationVolumeM3"])
        model.setSiliconMaterial(material(p["siliconMaterial"])); model.setOxideMaterial(material(p["oxideMaterial"]))
        if p["maskMaterial"]: model.setMaskMaterial(material(p["maskMaterial"]))
    elif kind == "oxideRegrowth":
        axis = 1 if project["dimension"] == 2 else 2
        top = max(float(point[axis]) for point in domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01).getNodes())
        height = top + max(1.0, 5 * float(project["domain"]["gridDelta"]))
        domain.generateCellSet(height, ps.Material.GAS, True)
        cell_set = domain.getCellSet(); cell_set.addScalarData("byproductSum", 0.0); cell_set.buildNeighborhood()
        model = ps.OxideRegrowth(*(p[key] for key in ("nitrideEtchRate", "oxideEtchRate", "redepositionRate", "redepositionThreshold", "redepositionTimeInt", "diffusionCoefficient", "sinkStrength", "scallopVelocity", "centerVelocity", "topHeight", "centerWidth", "stabilityFactor")))
    elif kind == "selectiveEpitaxy":
        rows = [(material(name), float(value)) for name, value in sorted(p["materialRates"].items())]
        model = ps.SelectiveEpitaxy(rows, p["rate111"], p["rate100"])
    elif kind == "neutralTransport":
        parameters = ps.NeutralTransportParameters()
        for key in ("incomingFlux", "sourceDistributionPower", "etchFrontSticking", "zeroCoverageSticking", "coverageTimeStep", "desorptionRate", "surfaceDiffusionCoefficient", "surfaceSiteDensity"):
            setattr(parameters, key, float(p[key]))
        parameters.useSteadyStateCoverage = bool(p["useSteadyStateCoverage"]); parameters.etchFrontMaterial = material(p["etchFrontMaterial"])
        parameters.fluxLabel = p["fluxLabel"]; parameters.coverageLabel = p["coverageLabel"]
        model = ps.NeutralTransport(parameters)
    else:
        raise ValueError(f"unknown reference model {kind}")
    return model, extra


def run(project: dict[str, Any], output: Path, scratch: Path, engine: str = "CPU_DISK") -> None:
    step = project["steps"][0]
    domain = make_domain(project)
    initial = snapshot(domain, project["dimension"])
    if step["operation"] == "deposit":
        deposit = ps.Material.SiGe if step["nativeModel"] == "selectiveEpitaxy" else material(step["depositMaterial"])
        domain.duplicateTopLevelSet(deposit)
    model, extra = make_model(step, domain, project)
    duration = float(step.get("duration", float(step["nativeParameters"].get("timeHours", 0.0)) * 3600.0))
    process = ps.Process(domain, model, duration)
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    advection = ps.AdvectionParameters(); advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, project["solver"]["advection"]["spatialScheme"])
    advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, project["solver"]["advection"]["temporalScheme"])
    for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities", "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
        setattr(advection, key, project["solver"]["advection"][key])
    if step["nativeModel"] == "oxideRegrowth": advection.ignoreVoids = True
    process.setParameters(advection)
    for item in extra: process.setParameters(item)
    flux_models = {"sf6o2", "cf4o2", "hbro2", "sf6c4f8", "fluorocarbon", "ionBeam", "teosDeposition", "teospecvd", "ald", "neutralTransport"}
    if step["nativeModel"] in flux_models:
        rays = ps.RayTracingParameters(); rays.raysPerPoint = int(project["solver"]["raysPerPoint"]); rays.rngSeed = int(project["solver"]["random"]["seed"]); rays.useRandomSeeds = False
        for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections", "minNodeDistanceFactor", "smoothingNeighbors"):
            setattr(rays, key, project["solver"]["rayTracing"][key])
        rays.normalizationType = getattr(ps.NormalizationType, project["solver"]["rayTracing"]["normalizationType"])
        process.setParameters(rays)
    previous = Path.cwd(); scratch.mkdir(parents=True, exist_ok=True)
    try:
        os.chdir(scratch); process.setIntermediateOutputPath(str(scratch)); process.apply()
    finally:
        os.chdir(previous)
    output.write_text(json.dumps({"status": "succeeded", "model": step["nativeModel"], "dimension": project["dimension"],
                                  "versions": {"viennaps": importlib.metadata.version("viennaps"), "viennals": importlib.metadata.version("viennals")},
                                  "initial": initial, "final": snapshot(domain, project["dimension"])}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5): raise SystemExit("usage: stage8_direct_reference.py INPUT.json OUTPUT.json SCRATCH_DIR [ENGINE]")
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run(value, Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] if len(sys.argv) == 5 else "CPU_DISK")
