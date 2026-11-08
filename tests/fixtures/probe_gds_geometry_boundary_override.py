"""Probe whether GDS level-set boundary flags cause the specialized periodic crash."""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests/fixtures"))
from gpu_geometry_direct_reference import snapshot
from stage8_direct_reference import make_model


def boundary(name: str):
    return ps.BoundaryType.REFLECTIVE_BOUNDARY if name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY


project_path, source_path, output_path = map(Path, sys.argv[1:4])
geometry_boundary_name = sys.argv[4]
engine_name = sys.argv[5] if len(sys.argv) > 5 else "CPU_DISK"
boundary_padding = float(sys.argv[6]) if len(sys.argv) > 6 else 0.0
boundary_padding_y = float(sys.argv[7]) if len(sys.argv) > 7 else boundary_padding
support_x_extent = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0
support_y_extent = float(sys.argv[9]) if len(sys.argv) > 9 else 0.0
restore_domain_setup = len(sys.argv) > 10 and sys.argv[10] == "restore-domain-setup"
project = json.loads(project_path.read_text(encoding="utf-8"))
source = source_path.resolve()
output = output_path.resolve()
dimension = int(project["dimension"])
settings = project["domain"]
geometry_spec = project["geometry"]
ps.Length.setUnit("um")
ps.Time.setUnit("s")
ps.setDimension(dimension)
ps.setNumThreads(int(project["solver"]["cpuThreads"]))
domain_boundary_name = settings.get("boundaryXY", settings.get("xBoundary"))
domain_edge = boundary(domain_boundary_name)
gds_edge = boundary(geometry_boundary_name)
if dimension == 2:
    domain = ps.d2.Domain(float(settings["gridDelta"]), float(settings["xExtent"]), domain_edge)
else:
    domain = ps.d3.Domain(float(settings["gridDelta"]), float(settings["xExtent"]),
                          float(settings["yExtent"]), domain_edge)
domain_bounds_before_gds = [float(value) for value in domain.getSetup().bounds()]

from backend.gds import parse_gds

library = parse_gds(source.read_bytes())
physical_scale = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
selected, _transform = library.select_and_transform(
    geometry_spec["structure"],
    unit_scale=physical_scale * float(geometry_spec["unitScale"]),
    position_um=geometry_spec["position"][:2],
    selected_layers=[int(row["layer"]) for row in geometry_spec["layers"]],
)
selected_path = output.with_suffix(".selected.gds")
selected_path.parent.mkdir(parents=True, exist_ok=True)
if support_x_extent > 0.0:
    selected_library = parse_gds(selected)
    selected_structure = selected_library.find_structure(geometry_spec["structure"])
    used_layers = {layer for item in selected_library.structures for layer in item.layers}
    support_layer = next(layer for layer in range(32767, -1, -1) if layer not in used_layers)
    bounds = [-support_x_extent / 2.0, support_x_extent / 2.0]
    physical_m_per_database_unit = selected_library.meters_per_database_unit
    to_dbu = lambda value: int(round(float(value) * 1e-6 / physical_m_per_database_unit))
    if dimension == 3 and support_y_extent > 0.0:
        y_bounds = [-support_y_extent / 2.0, support_y_extent / 2.0]
        xy = [(to_dbu(bounds[0]), to_dbu(y_bounds[0])),
              (to_dbu(bounds[1]), to_dbu(y_bounds[0])),
              (to_dbu(bounds[1]), to_dbu(y_bounds[1])),
              (to_dbu(bounds[0]), to_dbu(y_bounds[1])),
              (to_dbu(bounds[0]), to_dbu(y_bounds[0]))]
    else:
        y0 = to_dbu(min(row.get("baseHeight", 0.0) for row in geometry_spec["layers"]) + 0.5)
        y1 = y0 + max(1, to_dbu(float(settings["gridDelta"])))
        xy = [(to_dbu(bounds[0]), y0), (to_dbu(bounds[1]), y0),
              (to_dbu(bounds[1]), y1), (to_dbu(bounds[0]), y1), (to_dbu(bounds[0]), y0)]
    payload_xy = struct.pack(">" + "i" * (2 * len(xy)), *(coordinate for point in xy for coordinate in point))
    support = (struct.pack(">HBB", 4, 0x08, 0) + struct.pack(">HBBh", 6, 0x0D, 2, support_layer)
               + struct.pack(">HBBh", 6, 0x0E, 2, 0) + struct.pack(">HBB", len(payload_xy) + 4, 0x10, 3)
               + payload_xy + struct.pack(">HBB", 4, 0x11, 0))
    insert_at = selected_structure.end - 4
    selected = selected[:insert_at] + support + selected[insert_at:]
selected_path.write_bytes(selected)
if dimension == 2:
    geometry = ps.d2.GDSGeometry(float(settings["gridDelta"]))
    geometry.setBoundaryConditions([gds_edge, ps.BoundaryType.INFINITE_BOUNDARY])
    if boundary_padding or boundary_padding_y:
        geometry.setBoundaryPadding(boundary_padding, boundary_padding_y)
    ps.d2.GDSReader(geometry, str(selected_path)).apply()
    for row in geometry_spec["layers"]:
        handle = ps.MaterialRegistry.instance().getMaterial(row["material"])
        domain.insertNextLevelSetAsMaterial(geometry.layerToLevelSet(int(row["layer"]), True), handle, False)
else:
    geometry = ps.d3.GDSGeometry(float(settings["gridDelta"]))
    geometry.setBoundaryConditions([gds_edge, gds_edge, ps.BoundaryType.INFINITE_BOUNDARY])
    if boundary_padding or boundary_padding_y:
        geometry.setBoundaryPadding(boundary_padding, boundary_padding_y)
    ps.d3.GDSReader(geometry, str(selected_path)).apply()
    for row in geometry_spec["layers"]:
        handle = ps.MaterialRegistry.instance().getMaterial(row["material"])
        level_set = geometry.layerToLevelSet(int(row["layer"]), float(row.get("baseHeight", 0.0))
                                             + float(geometry_spec["position"][2]),
                                             float(row["height"]), bool(row.get("mask", False)), True)
        domain.insertNextLevelSetAsMaterial(level_set, handle, False)

step = project["steps"][0]
model, extras = make_model(step, domain, project)
process = ps.Process(domain, model, float(step["duration"]))
process.setFluxEngineType(getattr(ps.FluxEngineType, engine_name))
advection = ps.AdvectionParameters()
advection.timeStepRatio = float(project["solver"]["timeStepRatio"])
advection.spatialScheme = getattr(ps.ls.SpatialSchemeEnum, project["solver"]["advection"]["spatialScheme"])
advection.temporalScheme = getattr(ps.ls.TemporalSchemeEnum, project["solver"]["advection"]["temporalScheme"])
for key in ("adaptiveTimeStepping", "adaptiveTimeStepSubdivisions", "calculateIntermediateVelocities",
            "checkDissipation", "dissipationAlpha", "ignoreVoids", "velocityOutput"):
    setattr(advection, key, project["solver"]["advection"][key])
process.setParameters(advection)
for item in extras:
    process.setParameters(item)
rays = ps.RayTracingParameters()
rays.raysPerPoint = int(project["solver"]["raysPerPoint"])
rays.rngSeed = int(project["solver"]["random"]["seed"])
rays.useRandomSeeds = False
for key in ("diskRadius", "ignoreFluxBoundaries", "maxBoundaryHits", "maxReflections",
            "minNodeDistanceFactor", "smoothingNeighbors"):
    setattr(rays, key, project["solver"]["rayTracing"][key])
rays.normalizationType = getattr(ps.NormalizationType, project["solver"]["rayTracing"]["normalizationType"])
process.setParameters(rays)
initial = snapshot(domain, dimension)
geometry_bounds = [float(value) for value in geometry.getBounds()]
domain_bounds = [float(value) for value in domain.getSetup().bounds()]
if restore_domain_setup:
    if dimension == 2:
        domain.setup(float(settings["gridDelta"]), float(settings["xExtent"]), 0.0, domain_edge)
    else:
        domain.setup(float(settings["gridDelta"]), float(settings["xExtent"]),
                     float(settings["yExtent"]), domain_edge)
restored_domain_bounds = [float(value) for value in domain.getSetup().bounds()]
restored_initial = snapshot(domain, dimension)
initial_path = output.with_suffix(output.suffix + ".initial.json")
initial_path.write_text(json.dumps({"domainBoundary": domain_boundary_name,
                                   "domainBoundsBeforeGds": domain_bounds_before_gds,
                                   "gdsGeometryBoundary": geometry_boundary_name,
                                   "gdsBoundaryPadding": [boundary_padding, boundary_padding_y],
                                   "supportXExtent": support_x_extent,
                                   "supportYExtent": support_y_extent,
                                   "gdsGeometryBounds": geometry_bounds,
                                   "domainBounds": domain_bounds,
                                   "restoredDomainBounds": restored_domain_bounds,
                                   "restoreDomainSetup": restore_domain_setup,
                                   "initial": initial,
                                   "restoredInitial": restored_initial}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
process.apply()
final = snapshot(domain, dimension)
output.write_text(json.dumps({
    "status": "succeeded", "versions": {"viennaps": ps.__version__, "viennals": ps.ls.version},
    "engine": engine_name, "domainBoundary": domain_boundary_name,
    "domainBoundsBeforeGds": domain_bounds_before_gds,
    "gdsGeometryBoundary": geometry_boundary_name,
    "gdsBoundaryPadding": [boundary_padding, boundary_padding_y],
    "supportXExtent": support_x_extent,
    "supportYExtent": support_y_extent,
    "gdsGeometryBounds": geometry_bounds,
    "domainBounds": domain_bounds,
    "restoredDomainBounds": restored_domain_bounds,
    "restoreDomainSetup": restore_domain_setup,
    "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    "initial": initial, "restoredInitial": restored_initial, "final": final,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": "succeeded", "engine": engine_name, "domainBoundary": domain_boundary_name,
                  "gdsGeometryBoundary": geometry_boundary_name,
                  "initialPoints": len(initial["points"]), "finalPoints": len(final["points"])}))
