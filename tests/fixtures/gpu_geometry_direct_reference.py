"""Independent ViennaPS GPU_DISK references for schema-7 geometry presets.

This fixture builds preset geometries directly from the installed ViennaPS
bindings. It is deliberately independent from worker/simulate.py and must run
only under scripts/bounded_child.py.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage7_multiparticle_reference import (direct_material, snapshot as _snapshot, add_species,
                                            register_custom_materials)

NATIVE_MAKEFIN_SIGSEGV_MODELS = {"sf6o2", "hbro2", "sf6c4f8", "neutralTransport"}


def effective_fin_construction(project: dict[str, Any]) -> dict[str, Any]:
    geometry = project.get("geometry", {})
    requested = geometry.get("construction", "native") if geometry.get("kind") == "fin" else "native"
    enabled_models = {
        str(step.get("nativeModel")) for step in project.get("steps", [])
        if step.get("enabled") and step.get("model") == "specialized"
        and step.get("nativeModel") in NATIVE_MAKEFIN_SIGSEGV_MODELS
    }
    fallback_models = sorted(enabled_models) if requested == "native" else []
    settings = project.get("domain", {})
    boundary_name = settings.get("boundaryXY", settings.get("xBoundary", "reflective"))
    periodic_3d_fin = (project.get("dimension") == 3 and geometry.get("kind") == "fin"
                       and boundary_name == "periodic")
    generic_masked_models = sorted({
        str(step.get("model")) for step in project.get("steps", [])
        if step.get("enabled") and step.get("model") in {"singleParticle", "multiParticle"}
    }) if (project.get("dimension") == 3 and geometry.get("kind") == "fin"
           and float(geometry.get("maskHeight", 0)) > 0) else []
    periodic_masked_generic_models = generic_masked_models if periodic_3d_fin else []
    generic_construction_fallback_models = (generic_masked_models if requested == "native" else [])
    extrusion_fallback_models = sorted(enabled_models | set(periodic_masked_generic_models)) if (
        periodic_3d_fin and geometry.get("extrusion") is not None
    ) else []
    construction_fallback_models = sorted(set(fallback_models) | set(generic_construction_fallback_models))
    if generic_construction_fallback_models:
        effective = "boolean"
    elif fallback_models:
        effective = "boolean"
    else:
        effective = requested
    construction_fallback_reason = ("native-makefin-sigsegv" if fallback_models else
                                    "periodic-masked-fin-geometry" if periodic_3d_fin and generic_construction_fallback_models else
                                    "masked-3d-fin-generic-process" if generic_construction_fallback_models else None)
    extrusion_route = ("direct-stencil" if extrusion_fallback_models else "d2-extrude") \
        if geometry.get("extrusion") is not None else "direct"
    return {"requested": requested, "effective": effective,
            "fallbackModels": fallback_models,
            "constructionFallbackModels": construction_fallback_models,
            "constructionFallbackReason": construction_fallback_reason,
            "extrusionRoute": extrusion_route,
            "extrusionFallbackModels": extrusion_fallback_models}


def boolean_fin_3d(domain: Any, geometry: dict[str, Any], material: Any, mask_material: Any,
                   y_center: float, y_extent: float) -> None:
    ps.d3.MakePlane(domain, height=0.0, material=material, addToExisting=False).apply()
    factory = ps.d3.GeometryFactory(domain.getSetup(), "WorkbenchDirectBooleanFin")
    fin_stencil = factory.makeBoxStencil(
        [0.0, y_center, 0.0], 2.0 * float(geometry["width"]),
        float(geometry["height"]), -float(geometry["taperAngle"]), y_extent,
    )
    domain.applyBooleanOperation(fin_stencil, ps.ls.BooleanOperationEnum.UNION, False)
    mask_height = float(geometry.get("maskHeight", 0.0))
    if mask_height > 0:
        mask = factory.makeBoxStencil(
            [0.0, y_center, float(geometry["height"])],
            2.0 * float(geometry["width"]), mask_height,
            -float(geometry["maskTaperAngle"]), y_extent,
        )
        domain.insertNextLevelSetAsMaterial(mask, mask_material)


def snapshot(domain: Any, dimension: int) -> dict[str, Any]:
    # The workbench extracts the native material map before asking ViennaPS
    # for a surface mesh. Keep that order here: ViennaPS 4.6.2 lazily builds
    # parts of a 3D narrow band and the serialized state is order-sensitive.
    mapping = domain.getMaterialMap()
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    data = mesh.getCellData()
    material_ids = None
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "MaterialIds":
            material_ids = [int(value) for value in data.getScalarData(index)]
    if material_ids is None:
        raise RuntimeError("direct reference mesh has no MaterialIds cell data")
    cells = mesh.getLines() if dimension == 2 else mesh.getTriangles()
    points = [[float(value) for value in row] for row in mesh.getNodes()]
    return {
        "points": points,
        "cells": [[int(value) for value in row] for row in cells],
        "materialIds": material_ids,
        "materialIdsByLevelSetIndex": [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())],
        "levelSetCount": int(domain.getNumberOfLevelSets()),
    }


def boundary(name: str) -> Any:
    return ps.BoundaryType.REFLECTIVE_BOUNDARY if name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY


def make_domain(project: dict[str, Any], gds_source: Path | None = None,
                scratch: Path | None = None):
    dimension = int(project["dimension"])
    settings = project["domain"]
    edge = boundary(settings.get("xBoundary", settings.get("boundaryXY")))
    geometry = project["geometry"]
    kind = geometry["kind"]
    if kind == "gds":
        if gds_source is None or scratch is None:
            raise ValueError("GDS direct reference requires an explicit source and scratch directory")
        from stage5_geometry_reference import build_domain as build_gds_domain
        domain, readback = build_gds_domain(project, gds_source, scratch)
        readback.update({
            "sourceAssetId": geometry["assetId"],
            "selectedStructure": geometry["structure"],
            "selectedLayers": sorted(int(row["layer"]) for row in geometry["layers"]),
            "layerMappings": geometry["layers"],
        })
        return domain, readback
    # Construct exactly one native Domain for each direct-reference input.
    # The GDS branch above delegates its complete Domain + GDS setup to the
    # independent stage-5 constructor. Creating and discarding an empty
    # Domain before that call changes ViennaPS 4.6.2's cached 3D surface mesh
    # at reflective multi-level-set boundaries, although the material map and
    # GDS bounds remain identical.
    domain = (ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), edge)
              if dimension == 2 else
              ps.Domain(float(settings["gridDelta"]), float(settings["xExtent"]),
                        float(settings["yExtent"]), edge))
    material = direct_material(geometry["substrateMaterial"])
    if kind == "plane":
        ps.MakePlane(domain, height=float(geometry["height"]), material=material,
                     addToExisting=False).apply()
    elif kind == "trench":
        ps.MakeTrench(domain=domain, trenchWidth=float(geometry["width"]),
                      trenchDepth=float(geometry["depth"]), trenchTaperAngle=float(geometry["taperAngle"]),
                      maskHeight=float(geometry["maskHeight"]), maskTaperAngle=float(geometry["maskTaperAngle"]),
                      halfTrench=False, material=material,
                      maskMaterial=direct_material(geometry.get("maskMaterial", "Mask"))).apply()
    elif kind == "hole":
        if dimension != 3:
            raise ValueError("the installed geometry contract supports hole presets in 3D only")
        ps.MakeHole(domain=domain, holeRadius=float(geometry["radius"]),
                    holeDepth=float(geometry["depth"]), holeTaperAngle=float(geometry["taperAngle"]),
                    maskHeight=float(geometry["maskHeight"]), maskTaperAngle=float(geometry["maskTaperAngle"]),
                    holeShape=ps.HoleShape.FULL, material=material,
                    maskMaterial=direct_material(geometry.get("maskMaterial", "Mask"))).apply()
    elif kind == "fin":
        arguments = {
            "finWidth": float(geometry["width"]), "finHeight": float(geometry["height"]),
            "finTaperAngle": float(geometry["taperAngle"]), "maskHeight": float(geometry["maskHeight"]),
            "maskTaperAngle": float(geometry["maskTaperAngle"]), "halfFin": False,
            "material": material, "maskMaterial": direct_material(geometry["maskMaterial"]),
        }
        boolean_fin = (effective_fin_construction(project)["effective"] == "boolean"
                       or geometry.get("_booleanFinConstruction"))
        mask_material = direct_material(geometry["maskMaterial"])
        if boolean_fin and dimension == 2:
            ps.d2.MakePlane(domain, height=0.0, material=material, addToExisting=False).apply()
            factory = ps.d2.GeometryFactory(domain.getSetup(), "WorkbenchDirectBooleanFin")
            stencil = factory.makeBoxStencil([0.0, 0.0],
                                             float(geometry["width"]), float(geometry["height"]),
                                             -float(geometry["taperAngle"]))
            domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.UNION, False)
            mask_height = float(geometry.get("maskHeight", 0.0))
            if mask_height > 0:
                mask = factory.makeBoxStencil([0.0, float(geometry["height"])],
                                              float(geometry["width"]), mask_height,
                                              -float(geometry["maskTaperAngle"]))
                domain.insertNextLevelSetAsMaterial(mask, mask_material)
        elif dimension == 2:
            ps.d2.MakeFin(domain, **arguments).apply()
        elif (geometry.get("extrusion") is not None
              and effective_fin_construction(project)["extrusionRoute"] == "direct-stencil"):
            extent = [float(value) for value in geometry["extrusion"]["extent"]]
            boolean_fin_3d(domain, geometry, material, mask_material,
                           0.5 * (extent[0] + extent[1]), extent[1] - extent[0])
        elif geometry.get("extrusion") is not None:
            input_domain = ps.d2.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), edge)
            if boolean_fin:
                ps.d2.MakePlane(input_domain, height=0.0, material=material, addToExisting=False).apply()
                factory = ps.d2.GeometryFactory(input_domain.getSetup(), "WorkbenchDirectBooleanFin")
                stencil = factory.makeBoxStencil([0.0, 0.0],
                                                 float(geometry["width"]), float(geometry["height"]),
                                                 -float(geometry["taperAngle"]))
                input_domain.applyBooleanOperation(stencil, ps.ls.BooleanOperationEnum.UNION, False)
                mask_height = float(geometry.get("maskHeight", 0.0))
                if mask_height > 0:
                    mask = factory.makeBoxStencil([0.0, float(geometry["height"])],
                                                  float(geometry["width"]), mask_height,
                                                  -float(geometry["maskTaperAngle"]))
                    input_domain.insertNextLevelSetAsMaterial(mask, mask_material)
            else:
                ps.d2.MakeFin(input_domain, **arguments).apply()
            operation = ps.Extrude()
            operation.setInputDomain(input_domain)
            operation.setOutputDomain(domain)
            operation.setExtent([float(value) for value in geometry["extrusion"]["extent"]])
            operation.setExtrusionAxis(1)
            operation.setBoundaryConditions([edge, edge, ps.BoundaryType.INFINITE_BOUNDARY])
            operation.apply()
        else:
            if boolean_fin:
                boolean_fin_3d(domain, geometry, material, mask_material,
                               0.0, float(settings["yExtent"]))
            else:
                ps.d3.MakeFin(domain, **arguments).apply()
    else:
        raise ValueError(f"unsupported preset geometry {kind!r}")
    return domain, {}


def project_material_names(project: dict[str, Any]) -> list[str]:
    geometry = project["geometry"]
    if geometry["kind"] == "gds":
        names = {row["material"] for row in geometry["layers"]}
    else:
        names = {geometry["substrateMaterial"]}
    if geometry["kind"] in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0)) > 0:
        names.add(geometry.get("maskMaterial", "Mask"))
    step = project["steps"][0]
    if step.get("depositMaterial"):
        names.add(step["depositMaterial"])
    names.update(step.get("rateCoefficients", {}).get("byMaterial", {}).keys())
    for species in step.get("species", []):
        names.update(species.get("stickingProbabilityByMaterial", {}).keys())
    return sorted(names)


def apply_single_particle(domain: Any, project: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    sign = -1.0 if step["operation"] == "etch" else 1.0
    geometry = project["geometry"]
    mask_materials = []
    if geometry.get("kind") in {"trench", "hole"} and float(geometry.get("maskHeight", 0.0)) > 0:
        mask_materials = [direct_material(geometry.get("maskMaterial", "Mask"))]
    elif geometry.get("kind") == "gds":
        mask_materials = [direct_material(row["material"]) for row in geometry.get("layers", [])
                          if row.get("mask", False)]
    if step.get("rateMode") == "material":
        model = ps.SingleParticleProcess(
            rate=sign * float(step["rateMagnitude"]),
            stickingProbability=float(step["stickingProbability"]),
            sourceExponent=float(step["sourceExponent"]),
            maskMaterials=[],
        )
        for name, rate in (step.get("materialRates") or {}).items():
            model.setMaterialRate(direct_material(name), sign * float(rate))
    else:
        model = ps.SingleParticleProcess(
            rate=sign * float(step["rateMagnitude"]),
            stickingProbability=float(step["stickingProbability"]),
            sourceExponent=float(step["sourceExponent"]),
            maskMaterials=mask_materials,
        )
    process = ps.Process(domain, model, float(step["duration"]))
    return {"process": process, "callback": None}


def apply_multi_particle(domain: Any, project: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    model = ps.MultiParticleProcess()
    labels = add_species(model, step)
    relation = step["rateCoefficients"]
    defaults = {label: float(relation["defaultBySpecies"][label]) for label in labels}
    overrides = {name: {label: float(value) for label, value in row.items()}
                 for name, row in relation.get("byMaterial", {}).items()}
    material_names = {str(direct_material(name)): name for name in project_material_names(project)}
    callback: dict[str, Any] = {
        "count": 0,
        "materials": Counter(),
        "samples": [],
        "fluxSums": [0.0] * len(labels),
        "fluxMaxima": [0.0] * len(labels),
    }
    sign = -1.0 if step["operation"] == "etch" else 1.0

    def rate_function(fluxes: Any, native_material: Any) -> float:
        name = material_names.get(str(native_material))
        if name is None:
            raise RuntimeError(f"unregistered material in direct callback: {native_material}")
        values = [float(value) for value in fluxes]
        if len(values) != len(labels):
            raise RuntimeError("flux vector length differs from the configured species list")
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError("native callback returned a non-finite species flux")
        for index, value in enumerate(values):
            callback["fluxSums"][index] += value
            callback["fluxMaxima"][index] = max(callback["fluxMaxima"][index], value)
        coefficients = overrides.get(name, {})
        velocity = sign * sum(values[index] * coefficients.get(label, defaults[label])
                              for index, label in enumerate(labels))
        callback["count"] += 1
        callback["materials"][name] += 1
        if len(callback["samples"]) < 16:
            callback["samples"].append({
                "material": name, "fluxes": values,
                "coefficients": {label: coefficients.get(label, defaults[label]) for label in labels},
                "returnedVelocity": velocity,
            })
        return velocity

    model.setRateFunction(rate_function)
    process = ps.Process(domain, model, float(step["duration"]))
    native_species = []
    for species in step["species"]:
        if species["kind"] == "ion":
            parameter_keys = ("sourcePower", "thetaRMin", "thetaRMax", "minAngle", "B_sp", "meanEnergy",
                              "sigmaEnergy", "thresholdEnergy", "inflectAngle", "n")
            native_species.append({"kind": "ion", "label": species["label"],
                                   **{key: species[key] for key in parameter_keys}})
        else:
            sticking = species.get("stickingProbabilityByMaterial", {})
            native_species.append({
                "kind": "neutral", "label": species["label"],
                "defaultStickingProbability": float(species["defaultStickingProbability"]),
                "stickingProbabilityByMaterial": {name: float(value) for name, value in sticking.items()},
                "stickingMaterialIds": {name: int(direct_material(name).legacyId()) for name in sticking},
            })
    actual = {
        "speciesOrder": labels,
        "species": native_species,
        "rateFunction": {
            "kind": "weighted-sum-table",
            "formula": "operationSign * sum(flux[i] * coefficient[material][species[i]])",
            "operationSign": sign,
            "defaultBySpecies": defaults,
            "byMaterial": overrides,
            "nativeFluxUnits": "not-declared-by-installed-binding",
            "returnedVelocityUnit": "um/s workbench convention; coefficients perform the conversion",
        },
        "nativeSpeciesFieldNames": [f"{label}{index}" for index, label in enumerate(labels)],
    }
    return {"process": process, "callback": callback, "actualParameters": actual}


def run(project: dict[str, Any], output: Path, scratch: Path, engine: str = "GPU_DISK",
        gds_source: Path | None = None) -> None:
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    register_custom_materials(project.get("materialDefinitions", []))
    scratch.mkdir(parents=True, exist_ok=True)
    domain, geometry_readback = make_domain(project, gds_source, scratch)
    # Mirror the native readbacks performed by the product before its first
    # snapshot. They are deliberately explicit so regression evidence covers
    # the same ViennaPS 4.6.2 initialization path.
    domain.getGridDelta()
    domain.getBoundaryConditions()
    initial = snapshot(domain, dimension)
    if dimension == 3:
        domain.getBoundingBox()
    # Product runs persist the initialized Domain as a native .vpsd snapshot
    # before process.apply(); mirror that ViennaPS API call in the independent
    # native sequence so state persistence and processing occur in the same
    # order.
    scratch.mkdir(parents=True, exist_ok=True)
    initial_surface = scratch / "initial.vtk"
    domain.saveSurfaceMesh(str(initial_surface), addInterfaces=True, sharpCorners=False,
                           minNodeDistanceFactor=0.01)
    if not initial_surface.is_file() or initial_surface.stat().st_size <= 0:
        raise RuntimeError("direct ViennaPS failed to save the initial surface mesh")
    initial_state = scratch / "state-initial.vpsd"
    ps.Writer(domain, str(initial_state)).apply()
    if not initial_state.is_file() or initial_state.stat().st_size <= 0:
        raise RuntimeError("direct ViennaPS Writer did not persist the initial geometry")
    step = project["steps"][0]
    if step["operation"] == "deposit":
        domain.duplicateTopLevelSet(direct_material(step["depositMaterial"]))
    execution = (apply_single_particle(domain, project, step)
                 if step["model"] == "singleParticle"
                 else apply_multi_particle(domain, project, step))
    process = execution["process"]
    process.setFluxEngineType(getattr(ps.FluxEngineType, engine))
    advection = ps.AdvectionParameters()
    advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
    requested = project["solver"]["advection"]
    advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, requested["spatialScheme"])
    advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, requested["temporalScheme"])
    for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
                "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
        setattr(advection, key, requested[key])
    process.setParameters(advection)
    rays = ps.RayTracingParameters()
    rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
    rays.rngSeed = int(project["solver"]["random"]["seed"])
    rays.useRandomSeeds = False
    for key, value in project["solver"]["rayTracing"].items():
        if key == "normalizationType":
            rays.normalizationType = getattr(ps.NormalizationType, value)
        else:
            setattr(rays, key, value)
    process.setParameters(rays)
    scratch.mkdir(parents=True, exist_ok=True)
    prior = Path.cwd()
    try:
        import os
        os.chdir(scratch)
        process.setIntermediateOutputPath(str(scratch))
        process.apply()
    finally:
        import os
        os.chdir(prior)
    callback = execution["callback"]
    sign = -1.0 if step["operation"] == "etch" else 1.0
    result = {
        "status": "succeeded",
        "viennapsModulePath": str(Path(ps.__file__).resolve()),
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "actualEngine": engine,
        "seed": int(rays.rngSeed),
        "threads": int(project["solver"]["cpuThreads"]),
        "finConstruction": effective_fin_construction(project),
        "geometryReadback": geometry_readback,
        "actualParameters": {
            "rateMode": step.get("rateMode", "uniform"),
            "actualDefaultRate": sign * float(step.get("rateMagnitude", 0.0)),
            "actualMaterialRatesByName": {
                name: sign * float(value) for name, value in (step.get("materialRates") or {}).items()
            },
            **({"stickingProbability": float(step["stickingProbability"]),
                "sourceExponent": float(step["sourceExponent"])}
               if step["model"] == "singleParticle" else {}),
            **({"speciesOrder": execution["actualParameters"]["speciesOrder"],
                "species": execution["actualParameters"]["species"],
                "rateFunction": execution["actualParameters"]["rateFunction"]}
               if step["model"] == "multiParticle" else {}),
        },
        "initial": initial,
        "final": snapshot(domain, dimension),
        "callback": None if callback is None else {
            "count": callback["count"], "materials": dict(callback["materials"]),
            "samples": callback["samples"], "fluxSums": callback["fluxSums"],
            "fluxMaxima": callback["fluxMaxima"],
            "meanFluxBySpecies": [value / callback["count"] for value in callback["fluxSums"]],
        },
    }
    if callback is not None and callback["count"] <= 0:
        raise RuntimeError("ViennaPS did not invoke the direct MultiParticle rate callback")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5, 6):
        raise SystemExit("usage: gpu_geometry_direct_reference.py PROJECT.json OUTPUT.json SCRATCH_DIR [ENGINE [GDS_SOURCE]]")
    project_path, output_path, scratch_path = map(Path, sys.argv[1:4])
    run(json.loads(project_path.read_text(encoding="utf-8")), output_path, scratch_path,
        sys.argv[4] if len(sys.argv) >= 5 else "GPU_DISK",
        Path(sys.argv[5]) if len(sys.argv) == 6 else None)
