"""Independent direct ViennaPS reference for Stage 7 project JSON."""
from __future__ import annotations

import importlib.metadata
import hashlib
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

import viennaps as ps


def direct_material(name: str):
    material = ps.MaterialRegistry.instance().findMaterial(name)
    if material is None:
        raise RuntimeError(f"direct reference cannot find material {name!r}")
    return ps.MaterialRegistry.instance().getMaterial(name)


def register_custom_materials(rows):
    registry = ps.MaterialRegistry.instance()
    for index, row in enumerate(rows):
        material = registry.registerMaterial(row["name"])
        if material.customId() != index or material.legacyId() != row["id"]:
            raise RuntimeError(f"custom material ID mismatch for {row['name']}")
        info = registry.getInfo(material)
        info.color_hex = int(row["color"].lstrip("#"), 16)
        if row.get("category"):
            info.category = ps.MaterialCategory[str(row["category"])]
        if row.get("densityGcm3") is not None:
            info.density_gcm3 = float(row["densityGcm3"])
        info.conductive = bool(row.get("conductive", False))
        registry.setInfo(material, info)


def make_domain(project):
    dim = project["dimension"]
    settings = project["domain"]
    boundary_name = settings.get("xBoundary", settings.get("boundaryXY"))
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if dim == 2:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), boundary=boundary)
    else:
        domain = ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), float(settings["yExtent"]), boundary)
    geometry = project["geometry"]
    if geometry["kind"] == "plane":
        ps.MakePlane(domain, height=float(geometry.get("height", 0)),
                     material=direct_material(geometry["substrateMaterial"]), addToExisting=False).apply()
        return domain
    if geometry["kind"] != "stack":
        raise RuntimeError("Stage 7 direct fixture supports plane and acceptance stack geometries")
    ps.MakePlane(domain, height=float(geometry.get("baseHeight", 0)),
                 material=direct_material(geometry["substrateMaterial"]), addToExisting=False).apply()
    height = float(geometry.get("baseHeight", 0))
    for layer in geometry["layers"]:
        height += float(layer["thickness"])
        ps.MakePlane(domain, height=height, material=direct_material(layer["material"]), addToExisting=True).apply()
    pattern = geometry["pattern"]
    if pattern["kind"] == "trench":
        top_thickness = float(geometry["layers"][-1]["thickness"])
        eps = float(settings["gridDelta"]) * 1e-3
        position = [0.0] * dim
        position[-1] = height - top_thickness - eps
        factory = ps.GeometryFactory(domain.getSetup(), "WorkbenchStackPattern")
        stencil = factory.makeBoxStencil(position, float(pattern["width"]), top_thickness + 2 * eps,
                                         -float(pattern.get("taperAngle", 0)))
        domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT, False)
    elif pattern["kind"] != "none":
        raise RuntimeError("unsupported direct-reference stack pattern")
    return domain


def add_species(model, step):
    labels = []
    for species in step["species"]:
        label = species["label"]
        labels.append(label)
        if species["kind"] == "ion":
            model.addIonParticle(
                sourcePower=float(species["sourcePower"]), thetaRMin=float(species["thetaRMin"]),
                thetaRMax=float(species["thetaRMax"]), minAngle=float(species["minAngle"]),
                B_sp=float(species["B_sp"]), meanEnergy=float(species["meanEnergy"]),
                sigmaEnergy=float(species["sigmaEnergy"]), thresholdEnergy=float(species["thresholdEnergy"]),
                inflectAngle=float(species["inflectAngle"]), n=int(species["n"]), label=label)
        elif species["kind"] == "neutral":
            sticking = {direct_material(name): float(probability)
                        for name, probability in species.get("stickingProbabilityByMaterial", {}).items()}
            model.addNeutralParticle(sticking,
                                     defaultStickingProbability=float(species["defaultStickingProbability"]),
                                     label=label)
        else:
            raise RuntimeError(f"unsupported native species kind {species['kind']!r}")
    return labels


def snapshot(domain, dimension):
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=.01)
    data = mesh.getCellData()
    material_ids = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    if material_ids is None:
        raise RuntimeError("ViennaPS direct result has no MaterialIds cell array")
    mapping = domain.getMaterialMap()
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    cells = [[int(value) for value in row] for row in cells]
    if any(not math.isfinite(value) for row in points for value in row):
        raise RuntimeError("ViennaPS direct result contains non-finite coordinates")
    return {"points": points, "cells": cells, "materialIds": material_ids,
            "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
            "levelSetCount": int(domain.getNumberOfLevelSets())}


def apply_step(domain, project, step, engine="CPU_DISK"):
    table = step["rateCoefficients"]
    if not any(float(value) > 0 for value in table.get("defaultBySpecies", {}).values()) and not any(
        float(value) > 0 for row in table.get("byMaterial", {}).values() for value in row.values()
    ):
        return {"snapshot": snapshot(domain, project["dimension"]), "callback": {"count": 0, "materials": {}, "samples": []},
                "engine": "not-applicable", "seed": None, "threads": int(project["solver"]["cpuThreads"]), "noOp": True}
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(direct_material(step["depositMaterial"]))
    pre_process_snapshot = snapshot(domain, project["dimension"])
    model = ps.MultiParticleProcess()
    labels = add_species(model, step)
    relation = step["rateCoefficients"]
    defaults = {label: float(relation["defaultBySpecies"][label]) for label in labels}
    overrides = {material: {key: float(value) for key, value in row.items()}
                 for material, row in relation.get("byMaterial", {}).items()}
    material_names = {str(direct_material(name)): name for name in project_material_names(project)}
    callback = {"count": 0, "materials": Counter(), "samples": [], "minVelocity": None, "maxVelocity": None}
    sign = -1.0 if step["operation"] == "etch" else 1.0

    def rate_function(fluxes, material):
        name = material_names.get(str(material))
        if name is None:
            raise RuntimeError(f"unregistered material in native callback: {material}")
        values = [float(value) for value in fluxes]
        if len(values) != len(labels) or any(not math.isfinite(value) for value in values):
            raise RuntimeError(f"native callback flux vector length {len(values)} does not match {len(labels)} species {labels}: {values}")
        specific = overrides.get(name, {})
        coefficients = {label: specific.get(label, defaults[label]) for label in labels}
        velocity = sign * sum(values[index] * coefficients[label] for index, label in enumerate(labels))
        callback["count"] += 1
        callback["materials"][name] += 1
        callback["minVelocity"] = velocity if callback["minVelocity"] is None else min(callback["minVelocity"], velocity)
        callback["maxVelocity"] = velocity if callback["maxVelocity"] is None else max(callback["maxVelocity"], velocity)
        if len(callback["samples"]) < 256:
            sample = {"material": name, "fluxes": values,
                      "coefficients": coefficients, "returnedVelocity": velocity}
            if os.environ.get("VIENNAPS_DEBUG_NATIVE_CALLBACKS") == "1":
                sample["nativeMaterial"] = str(material)
            callback["samples"].append(sample)
        return velocity

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    requested_advection = project["solver"].get("advection")
    if requested_advection:
        advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested_advection["spatialScheme"])
        advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested_advection["temporalScheme"])
        for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                    "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
            setattr(advection, key, requested_advection[key])
    process.setParameters(advection)
    ray = ps.RayTracingParameters()
    ray_settings = project["solver"].get("rayTracing", {})
    ray.raysPerPoint = int(project["solver"]["raysPerPoint"])
    ray.rngSeed = int(project["solver"]["random"]["seed"])
    ray.useRandomSeeds = False
    for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
                "minNodeDistanceFactor", "smoothingNeighbors"):
        if key in ray_settings:
            setattr(ray, key, ray_settings[key])
    if "normalizationType" in ray_settings:
        ray.normalizationType = getattr(ps.NormalizationType, ray_settings["normalizationType"])
    process.setParameters(ray)
    previous_cwd = Path.cwd()
    intermediate = os.environ.get("VIENNAPS_DIRECT_INTERMEDIATE_DIR")
    if intermediate:
        intermediate_path = Path(intermediate).resolve()
        intermediate_path.mkdir(parents=True, exist_ok=True)
        process.setIntermediateOutputPath(str(intermediate_path))
        os.chdir(intermediate_path)
    try:
        process.apply()
    finally:
        if intermediate:
            os.chdir(previous_cwd)
    if callback["count"] <= 0:
        raise RuntimeError("ViennaPS did not call the configured rate function")
    if step["operation"] == "etch" and not callback["maxVelocity"] < 0:
        raise RuntimeError("etch callback did not return negative native velocities")
    return {"snapshot": snapshot(domain, project["dimension"]),
            "preProcessSnapshot": pre_process_snapshot,
            "callback": {**callback, "materials": dict(callback["materials"])},
            "engine": engine, "seed": int(ray.rngSeed), "threads": int(project["solver"]["cpuThreads"]), "noOp": False}


def project_material_names(project):
    names = {project["geometry"]["substrateMaterial"]}
    names.update(layer["material"] for layer in project["geometry"].get("layers", []))
    for step in project["steps"]:
        if step.get("depositMaterial"):
            names.add(step["depositMaterial"])
        for species in step.get("species", []):
            names.update(species.get("stickingProbabilityByMaterial", {}).keys())
    for step in project["steps"]:
        names.update(step.get("rateCoefficients", {}).get("byMaterial", {}).keys())
    return sorted(names)


def main(project_path, output_path, engine="CPU_DISK", initial_state_path=None):
    project = json.loads(Path(project_path).read_text(encoding="utf-8"))
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    ps.setDimension(int(project["dimension"]))
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    register_custom_materials(project.get("materialDefinitions", []))
    if initial_state_path:
        boundary_name = project["domain"].get("xBoundary", project["domain"].get("boundaryXY"))
        boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if boundary_name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
        if project["dimension"] == 2:
            domain = ps.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]), boundary=boundary)
        else:
            domain = ps.Domain(float(project["domain"]["gridDelta"]), float(project["domain"]["xExtent"]),
                               float(project["domain"]["yExtent"]), boundary)
        ps.Reader(domain, str(Path(initial_state_path).resolve())).apply()
        state_info = {"sha256": hashlib.sha256(Path(initial_state_path).read_bytes()).hexdigest(),
                      "loadedWith": "viennaps.Reader", "materialIdsByLevelSetIndex":
                      [int(domain.getMaterialMap().getMaterialIdAtIdx(i)) for i in range(domain.getMaterialMap().size())],
                      "levelSetCount": int(domain.getNumberOfLevelSets())}
    else:
        domain = make_domain(project)
        state_info = None
    results = {"viennapsModulePath": ps.__file__, "versions": {"viennaps": importlib.metadata.version("viennaps"),
                            "viennals": importlib.metadata.version("viennals")},
               "initial": snapshot(domain, project["dimension"]), "steps": {}}
    if state_info:
        results["loadedNativeState"] = state_info
    for step in project["steps"]:
        if step.get("enabled"):
            results["steps"][step["id"]] = apply_step(domain, project, step, engine)
    Path(output_path).write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"versions": results["versions"], "dimension": project["dimension"],
                      "steps": {name: {"points": len(value["snapshot"]["points"]),
                                       "callbacks": value["callback"]["count"],
                                       "materials": value["callback"]["materials"],
                                       "minVelocity": value["callback"].get("minVelocity"),
                                       "maxVelocity": value["callback"].get("maxVelocity")}
                                for name, value in results["steps"].items()}}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4, 5): raise SystemExit("usage: stage7_multiparticle_reference.py PROJECT.json OUTPUT.json [ENGINE [INITIAL_STATE.vpsd]]")
    main(*sys.argv[1:])
