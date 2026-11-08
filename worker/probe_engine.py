"""Bounded, one-shot probes for the installed ViennaPS package.

Invoke only through scripts/bounded_child.py. This file is deliberately not
imported by the API process.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from importlib.metadata import version
from pathlib import Path


def enum_members(enum_type):
    return {name: int(value) for name, value in enum_type.__members__.items()}


def mesh_report(mesh):
    nodes = mesh.getNodes()
    lines = mesh.getLines()
    point_data = mesh.getPointData()
    cell_data = mesh.getCellData()
    point_scalars = {}
    for index in range(point_data.getScalarDataSize()):
        label = point_data.getScalarDataLabel(index)
        point_scalars[label] = point_data.getScalarData(index)
    cell_scalars = {}
    for index in range(cell_data.getScalarDataSize()):
        label = cell_data.getScalarDataLabel(index)
        cell_scalars[label] = cell_data.getScalarData(index)
    return {
        "pointCount": len(nodes),
        "lineCount": len(lines),
        "nodes": nodes,
        "lines": lines,
        "pointScalarData": point_scalars,
        "cellScalarData": cell_scalars,
        "pointVectorCount": point_data.getVectorDataSize(),
        "cellVectorCount": cell_data.getVectorDataSize(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "geometry", "etch", "deposit", "particle", "two-deposits", "two-deposits-etch"))
    parser.add_argument("--delta", type=float, default=0.1)
    parser.add_argument("--extent", type=float, default=4.0)
    parser.add_argument("--y-extent", type=float)
    parser.add_argument("--width", type=float, default=1.2)
    parser.add_argument("--depth", type=float, default=1.0)
    parser.add_argument("--angle", type=float, default=0.0)
    parser.add_argument("--mask-height", type=float, default=0.0)
    parser.add_argument("--mask-angle", type=float, default=0.0)
    parser.add_argument("--boundary", choices=("reflective", "periodic"), default="reflective")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--rate", type=float, default=0.2)
    parser.add_argument("--duration", type=float, default=0.2)
    parser.add_argument("--rays", type=int, default=128)
    parser.add_argument("--seed", type=int, default=314159)
    parser.add_argument("--sticking", type=float, default=0.5)
    parser.add_argument("--source-exponent", type=float, default=1.0)
    parser.add_argument("--time-step-ratio", type=float, default=0.25)
    parser.add_argument("--include-levelset-meshes", action="store_true")
    parser.add_argument("--include-material-map", action="store_true")
    parser.add_argument("--include-hull", action="store_true")
    parser.add_argument("--include-multisurface", action="store_true")
    parser.add_argument("--save-vtk", type=Path)
    parser.add_argument("--sharp-corners", action="store_true")
    parser.add_argument("--min-node-distance-factor", type=float, default=0.01)
    args = parser.parse_args()

    import viennaps as ps

    ps.setDimension(2)
    ps.setNumThreads(args.threads)
    package_version = version("viennaps")
    ls_version = version("viennals")
    if args.mode == "inspect":
        gpu_available = None
        gpu_error = None
        try:
            gpu_available = bool(ps.gpuAvailable())
        except Exception as exc:
            gpu_error = f"{type(exc).__name__}: {exc}"
        registry = ps.MaterialRegistry.instance()
        material_names = {str(int(material)): registry.getName(material) for material in (ps.Material.Mask, ps.Material.Si, ps.Material.SiO2)}
        result = {
            "viennapsVersion": package_version,
            "viennalsVersion": ls_version,
            "python": __import__("sys").version,
            "boundaryType": enum_members(ps.BoundaryType),
            "fluxEngineType": enum_members(ps.FluxEngineType),
            "materials": {name: int(value) for name, value in ps.BuiltInMaterial.__members__.items()},
            "dimension": 2,
            "gpuAvailable": gpu_available,
            "gpuProbeError": gpu_error,
            "materialNames": material_names,
            "domainSignatures": [str(item) for item in getattr(ps.Domain.__init__, "__doc__", "").splitlines() if item.strip()],
            "meshGetSurfaceSignature": str(getattr(ps.Domain.getSurfaceMesh, "__doc__", "")),
        }
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0

    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if args.boundary == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    if args.y_extent is None:
        domain = ps.Domain(args.delta, args.extent, boundary)
    else:
        domain = ps.Domain(args.delta, args.extent, args.y_extent, boundary)
    if args.mode == "geometry":
        ps.MakeTrench(
            domain=domain,
            trenchWidth=args.width,
            trenchDepth=args.depth,
            trenchTaperAngle=args.angle,
            maskHeight=args.mask_height,
            maskTaperAngle=args.mask_angle,
            halfTrench=False,
            material=ps.Material.Si,
            maskMaterial=ps.Material.Mask,
        ).apply()
        mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=args.sharp_corners, minNodeDistanceFactor=args.min_node_distance_factor)
        extra = {}
        if args.include_levelset_meshes:
            print("probe-marker: level-set-meshes", flush=True)
            extra["levelSetMeshes"] = [mesh_report(layer) for layer in domain.getLevelSetMesh(width=1)]
        if args.include_material_map:
            print("probe-marker: material-map", flush=True)
            material_map = domain.getMaterialMap()
            nested_map = material_map.getMaterialMap()
            extra["materialMap"] = {
                "wrapperType": type(material_map).__name__,
                "levelSetCount": material_map.size(),
                "materialIdsByIndex": [int(material_map.getMaterialIdAtIdx(index)) for index in range(material_map.size())],
                "nestedMapType": type(nested_map).__name__,
                "nestedMaterialIds": nested_map.getMaterialMap(),
                "nestedLayerCount": nested_map.getNumberOfLayers(),
            }
        if args.include_hull:
            print("probe-marker: hull-mesh", flush=True)
            extra["hullMesh"] = mesh_report(domain.getHullMesh(bottomExtension=0.0, sharpCorners=False))
        if args.include_multisurface:
            print("probe-marker: direct-multisurface", flush=True)
            multi_mesh = ps.ls.Mesh()
            converter = ps.ls.ToMultiSurfaceMesh(
                domain.getLevelSets(), multi_mesh, minNodeDistFactor=args.min_node_distance_factor, eps=1e-12
            )
            converter.setMaterialMap(domain.getMaterialMap().getMaterialMap())
            converter.apply()
            extra["directMultiSurfaceMesh"] = mesh_report(multi_mesh)
        if args.save_vtk:
            output_path = args.save_vtk.resolve()
            output_path.relative_to(Path(__file__).resolve().parents[1])
            domain.saveSurfaceMesh(str(output_path), addInterfaces=True, sharpCorners=args.sharp_corners, minNodeDistanceFactor=args.min_node_distance_factor)
            extra["vtkOutput"] = {"path": str(output_path), "bytes": output_path.stat().st_size}
        result = {
            "mode": args.mode,
            "versions": {"viennaps": package_version, "viennals": ls_version},
            "requested": {"gridDelta": args.delta, "xExtent": args.extent, "boundary": args.boundary, "width": args.width, "depth": args.depth, "angle": args.angle, "maskHeight": args.mask_height, "maskAngle": args.mask_angle},
            "readback": {
                "gridDelta": domain.getGridDelta(),
                "boundingBox": domain.getBoundingBox(),
                "boundaryConditions": [str(value) for value in domain.getBoundaryConditions()],
                "levelSetCount": domain.getNumberOfLevelSets(),
                "materialsInDomain": [int(value) for value in domain.getMaterialsInDomain()],
            },
            "surfaceMesh": mesh_report(mesh),
            **extra,
        }
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0

    ps.MakePlane(domain, height=0.0, material=ps.Material.Si, addToExisting=False).apply()
    before_levels = domain.getNumberOfLevelSets()
    before_materials = sorted(int(value) for value in domain.getMaterialsInDomain())
    models = []
    repetitions = 2 if args.mode == "two-deposits" else 3 if args.mode == "two-deposits-etch" else 1
    for index in range(repetitions):
        deposition = args.mode == "deposit" or (args.mode in ("two-deposits", "two-deposits-etch") and index < 2)
        single_particle = args.mode == "particle"
        actual_rate = abs(args.rate) * (1 if deposition else -1)
        if deposition:
            domain.duplicateTopLevelSet(ps.Material.SiO2)
        if single_particle:
            model = ps.SingleParticleProcess(
                rate=actual_rate,
                stickingProbability=args.sticking,
                sourceExponent=args.source_exponent,
                maskMaterial=ps.Material.Mask,
            )
        else:
            model = ps.IsotropicProcess(rate=actual_rate, maskMaterial=ps.Material.Mask)
        process = ps.Process(domain, model, args.duration)
        process.setFluxEngineType(ps.FluxEngineType.CPU_DISK)
        advection = ps.AdvectionParameters()
        advection.timeStepRatio = args.time_step_ratio
        process.setParameters(advection)
        if single_particle:
            rays = ps.RayTracingParameters()
            rays.raysPerPoint = args.rays
            rays.rngSeed = args.seed
            rays.useRandomSeeds = False
            process.setParameters(rays)
        started = time.perf_counter()
        process.apply()
        elapsed = time.perf_counter() - started
        mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
        step_material_map = None
        if args.include_material_map:
            layer_map = domain.getMaterialMap()
            step_material_map = [int(layer_map.getMaterialIdAtIdx(i)) for i in range(layer_map.size())]
        models.append({
            "step": index,
            "operation": "deposit" if deposition else "etch",
            "actualRate": actual_rate,
            "duration": args.duration,
            "elapsedSeconds": elapsed,
            "advectionParameters": advection.toMetaData(),
            "rayTracingParameters": rays.toMetaData() if single_particle else None,
            "gridDeltaReadback": domain.getGridDelta(),
            "levelSetCount": domain.getNumberOfLevelSets(),
            "materialsInDomain": sorted(int(value) for value in domain.getMaterialsInDomain()),
            "materialIdsByLevelSetIndex": step_material_map,
            "surfaceMesh": mesh_report(mesh),
        })
    result = {
        "mode": args.mode,
        "versions": {"viennaps": package_version, "viennals": ls_version},
        "before": {"gridDelta": args.delta, "levelSetCount": before_levels, "materialsInDomain": before_materials},
        "steps": models,
        "final": {"levelSetCount": domain.getNumberOfLevelSets(), "materialsInDomain": sorted(int(value) for value in domain.getMaterialsInDomain())},
    }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise
