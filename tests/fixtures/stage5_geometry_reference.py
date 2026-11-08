"""Independent ViennaPS geometry/GDS/Slice reference for Stage 5.

Invoke this script only as a child of scripts/bounded_child.py. The native
geometry construction below deliberately calls the installed bindings
directly; it does not call worker.simulate.make_v3_geometry.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def material(name: str):
    registry = ps.MaterialRegistry.instance()
    found = registry.findMaterial(name)
    if found is None:
        raise ValueError(f"native material {name!r} is unavailable")
    return registry.getMaterial(name)


def surface(domain: object, dimension: int) -> dict:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    cells = {"lines": [[int(v) for v in row] for row in mesh.getLines()],
             "triangles": [[int(v) for v in row] for row in mesh.getTriangles()]}
    cell_data = mesh.getCellData()
    scalars = {}
    for index in range(cell_data.getScalarDataSize()):
        label = str(cell_data.getScalarDataLabel(index))
        scalars[label] = [float(value) for value in cell_data.getScalarData(index)]
    mapping = domain.getMaterialMap()
    materials = [int(mapping.getMaterialIdAtIdx(i)) for i in range(mapping.size())]
    return {
        "dimension": dimension,
        "points": [[float(v) for v in row] for row in mesh.getNodes()],
        "topology": {key: value for key, value in cells.items() if value},
        "cellScalars": scalars,
        "materialIdsByLevelSetIndex": materials,
    }


def boundary(name: str):
    return ps.BoundaryType.REFLECTIVE_BOUNDARY if name == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY


def _gds_layer_body(domain, planar, base_height: float, height: float, mask_tone: bool):
    """Reference implementation of a finite GDS layer on the simulation grid."""
    base_height = round(float(base_height), 12)
    top = round(base_height + float(height), 12)
    extruded = ps.ls.d3.Domain(float(domain.getSetup().gridDelta()))
    ps.ls.Extrude(planar, extruded, [base_height, top], 2,
                  list(domain.getSetup().boundaryCons())).apply()
    upper = ps.ls.d3.Domain(extruded)
    lower = ps.ls.d3.Domain(extruded)
    ps.ls.d3.MakeGeometry(upper, ps.ls.d3.Plane([0.0, 0.0, top], [0.0, 0.0, 1.0])).apply()
    ps.ls.d3.MakeGeometry(lower, ps.ls.d3.Plane([0.0, 0.0, base_height], [0.0, 0.0, 1.0])).apply()
    ps.ls.d3.BooleanOperation(upper, lower, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
    ps.ls.d3.BooleanOperation(extruded, upper, ps.ls.BooleanOperationEnum.INTERSECT).apply()
    if mask_tone:
        ps.ls.d3.BooleanOperation(
            upper, extruded, ps.ls.BooleanOperationEnum.RELATIVE_COMPLEMENT).apply()
        extruded = upper
    return extruded


def build_domain(project: dict, gds_source: Path | None, run_dir: Path):
    dimension = int(project["dimension"])
    ps.setDimension(dimension)
    ps.setNumThreads(int(project["solver"]["cpuThreads"]))
    d = project["domain"]
    edge = boundary(d.get("xBoundary", d.get("boundaryXY")))
    if dimension == 2:
        domain = ps.Domain(float(d["gridDelta"]), float(d["xExtent"]), edge)
    else:
        domain = ps.Domain(float(d["gridDelta"]), float(d["xExtent"]), float(d["yExtent"]), edge)
    g = project["geometry"]
    if g["kind"] == "plane":
        ps.MakePlane(domain, height=float(g["height"]), material=material(g["substrateMaterial"]),
                     addToExisting=False).apply()
    elif g["kind"] == "trench":
        ps.MakeTrench(domain, trenchWidth=float(g["width"]), trenchDepth=float(g["depth"]),
                      trenchTaperAngle=float(g["taperAngle"]), maskHeight=float(g["maskHeight"]),
                      maskTaperAngle=float(g["maskTaperAngle"]), halfTrench=bool(g.get("halfTrench")),
                      material=material(g["substrateMaterial"]),
                      maskMaterial=material(g.get("maskMaterial", "Mask"))).apply()
    elif g["kind"] == "hole":
        shape = str(g.get("holeShape") or "FULL")
        ps.MakeHole(domain, holeRadius=float(g["radius"]), holeDepth=float(g["depth"]),
                    holeTaperAngle=float(g["taperAngle"]), maskHeight=float(g["maskHeight"]),
                    maskTaperAngle=float(g["maskTaperAngle"]), holeShape=getattr(ps.HoleShape, shape),
                    material=material(g["substrateMaterial"]),
                    maskMaterial=material(g.get("maskMaterial", "Mask"))).apply()
    elif g["kind"] == "fin":
        half_fin = bool(g.get("halfFin"))
        args = {"finWidth": float(g["width"]), "finHeight": float(g["height"]),
                "finTaperAngle": float(g["taperAngle"]), "maskHeight": float(g["maskHeight"]),
                "maskTaperAngle": float(g["maskTaperAngle"]), "halfFin": half_fin,
                "material": material(g["substrateMaterial"]), "maskMaterial": material(g["maskMaterial"])}
        if dimension == 2:
            ps.d2.MakeFin(domain, **args).apply()
        elif g.get("extrusion"):
            input_domain = ps.d2.Domain(float(d["gridDelta"]), float(d["xExtent"]), edge)
            ps.d2.MakeFin(input_domain, **args).apply()
            operation = ps.Extrude()
            operation.setInputDomain(input_domain)
            operation.setOutputDomain(domain)
            operation.setExtent([float(value) for value in g["extrusion"]["extent"]])
            operation.setExtrusionAxis(1)
            operation.setBoundaryConditions([edge, edge, ps.BoundaryType.INFINITE_BOUNDARY])
            operation.apply()
        else:
            ps.d3.MakeFin(domain, **args).apply()
    elif g["kind"] == "gds":
        if gds_source is None:
            raise ValueError("GDS project requires source bytes")
        from backend.gds import add_periodic_extent_support, parse_gds
        library = parse_gds(gds_source.read_bytes())
        scale = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
        selected, transform = library.select_and_transform(
            g["structure"], unit_scale=scale * float(g["unitScale"]),
            position_um=g["position"][:2], selected_layers=[int(row["layer"]) for row in g["layers"]])
        periodic_support = {}
        if d.get("xBoundary", d.get("boundaryXY")) == "periodic":
            delta = float(d["gridDelta"])
            periodic = ps.BoundaryType.PERIODIC_BOUNDARY
            probe_path = run_dir / "direct-periodic-bounds-probe.gds"
            probe_path.write_bytes(selected)
            if dimension == 2:
                probe = ps.d2.GDSGeometry(delta)
                probe.setBoundaryConditions([periodic, ps.BoundaryType.INFINITE_BOUNDARY])
                ps.d2.GDSReader(probe, str(probe_path)).apply()
                bounds = [float(value) for value in probe.getBounds()]
                if bounds[0] < -float(d["xExtent"]) / 2 - delta * 1e-6 or bounds[1] > float(d["xExtent"]) / 2 + delta * 1e-6:
                    raise RuntimeError("periodic GDS geometry extends outside the requested x domain")
                selected, periodic_support = add_periodic_extent_support(
                    selected, g["structure"], dimension=2, x_extent_um=float(d["xExtent"]),
                    cross_section_y_bounds_um=(bounds[2], bounds[3]), grid_delta_um=delta)
            else:
                probe = ps.d3.GDSGeometry(delta)
                probe.setBoundaryConditions([periodic, periodic, ps.BoundaryType.INFINITE_BOUNDARY])
                ps.d3.GDSReader(probe, str(probe_path)).apply()
                bounds = [float(value) for value in probe.getBounds()]
                if (bounds[0] < -float(d["xExtent"]) / 2 - delta * 1e-6
                        or bounds[1] > float(d["xExtent"]) / 2 + delta * 1e-6
                        or bounds[2] < -float(d["yExtent"]) / 2 - delta * 1e-6
                        or bounds[3] > float(d["yExtent"]) / 2 + delta * 1e-6):
                    raise RuntimeError("periodic GDS geometry extends outside the requested XY domain")
                selected, periodic_support = add_periodic_extent_support(
                    selected, g["structure"], dimension=3, x_extent_um=float(d["xExtent"]),
                    y_extent_um=float(d["yExtent"]), grid_delta_um=delta)
            del probe
            probe_path.unlink(missing_ok=True)
        selected_path = run_dir / "direct-selected.gds"
        selected_path.write_bytes(selected)
        if dimension == 2:
            geometry = ps.d2.GDSGeometry(float(d["gridDelta"]))
            geometry.setBoundaryConditions([edge, ps.BoundaryType.INFINITE_BOUNDARY])
            ps.d2.GDSReader(geometry, str(selected_path)).apply()
            native_bounds = [float(value) for value in geometry.getBounds()]
            tolerance = max(1e-7, float(periodic_support.get("coordinateToleranceUm", 0.0)) * 1.1)
            if periodic_support and (abs(native_bounds[0] + float(d["xExtent"]) / 2.0) > tolerance
                                     or abs(native_bounds[1] - float(d["xExtent"]) / 2.0) > tolerance):
                raise RuntimeError("direct periodic 2D GDS bounds do not match the requested x period")
            native_layers = sorted(int(v) for v in geometry.getAllLayers())
            if sorted(set(native_layers) & {int(row["layer"]) for row in g["layers"]}) != sorted(int(row["layer"]) for row in g["layers"]):
                raise RuntimeError("native 2D GDS layers differ from the requested selection")
            for row in g["layers"]:
                level_set = geometry.layerToLevelSet(int(row["layer"]), True)
                # All layers share this native GDS grid; wrap cumulatively so the
                # top level set still contains every earlier material.
                domain.insertNextLevelSetAsMaterial(level_set, material(row["material"]), True)
        else:
            geometry = ps.d3.GDSGeometry(float(d["gridDelta"]))
            geometry.setBoundaryConditions([edge, edge, ps.BoundaryType.INFINITE_BOUNDARY])
            ps.d3.GDSReader(geometry, str(selected_path)).apply()
            native_bounds = [float(value) for value in geometry.getBounds()]
            tolerance = max(1e-7, float(periodic_support.get("coordinateToleranceUm", 0.0)) * 1.1)
            if periodic_support and (abs(native_bounds[0] + float(d["xExtent"]) / 2.0) > tolerance
                                     or abs(native_bounds[1] - float(d["xExtent"]) / 2.0) > tolerance
                                     or abs(native_bounds[2] + float(d["yExtent"]) / 2.0) > tolerance
                                     or abs(native_bounds[3] - float(d["yExtent"]) / 2.0) > tolerance):
                raise RuntimeError("direct periodic 3D GDS bounds do not match the requested XY periods")
            native_layers = sorted(int(v) for v in geometry.getAllLayers())
            if sorted(set(native_layers) & {int(row["layer"]) for row in g["layers"]}) != sorted(int(row["layer"]) for row in g["layers"]):
                raise RuntimeError("native 3D GDS layers differ from the requested selection")
            planar_geometry = ps.d2.GDSGeometry(float(d["gridDelta"]))
            planar_geometry.setBoundaryConditions([edge, edge])
            ps.d2.GDSReader(planar_geometry, str(selected_path)).apply()
            for row in g["layers"]:
                planar = planar_geometry.layerToLevelSet(int(row["layer"]), True)
                level_set = _gds_layer_body(
                    domain, planar, float(row["baseHeight"]) + float(g["position"][2]),
                    float(row["height"]), bool(row.get("mask", False)))
                # Cumulative native union on the shared simulation grid.
                domain.insertNextLevelSetAsMaterial(level_set, material(row["material"]), True)
        return domain, {"selectedFileSha256": hashlib.sha256(selected).hexdigest(),
                        "includedStructures": transform["includedStructures"],
                        "nativeLayers": native_layers,
                        "selectedNativeLayers": sorted(set(native_layers) & {int(row["layer"]) for row in g["layers"]}),
                        "nativeBounds": [float(v) for v in geometry.getBounds()], **periodic_support}
    else:
        raise ValueError(f"unsupported direct reference geometry {g['kind']!r}")
    return domain, {}


def main() -> None:
    request_path, output_path = (Path(item).resolve() for item in sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    project = request["project"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(output_path.parent)
    source = Path(request["gdsPath"]).resolve() if request.get("gdsPath") else None
    domain, readback = build_domain(project, source, output_path.parent)
    initial = surface(domain, int(project["dimension"]))
    result = {"versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
              "initial": initial, "materialIdsByLevelSetIndex": [int(domain.getMaterialMap().getMaterialIdAtIdx(i)) for i in range(domain.getMaterialMap().size())],
              "geometryReadback": readback}
    if request.get("sliceRequest") is not None:
        if int(project["dimension"]) != 3:
            raise ValueError("native Slice reference requires 3D source")
        slicing = request["sliceRequest"]
        axis_label = slicing.get("sliceAxis") or "y"
        axis = {"x": 0, "y": 1, "z": 2}[axis_label]
        reflect = bool(slicing.get("reflectX", False))
        ps.setDimension(2)
        d = project["domain"]
        lateral_extent = float(d["yExtent"] if axis == 0 else d["xExtent"])
        sliced = ps.d2.Domain(float(d["gridDelta"]), lateral_extent, boundary(d["boundaryXY"]))
        operation = ps.Slice(domain, sliced, axis, float(slicing["position"]))
        if reflect:
            operation.setReflectX(True)
        operation.apply()
        result["slice"] = surface(sliced, 2)
        result["sliceReadback"] = {"nativeOperation": "viennaps.Slice", "axis": axis,
                                   "axisName": axis_label, "reflectX": reflect,
                                   "position": float(slicing["position"]),
                                   "materialIdsByLevelSetIndex": [int(sliced.getMaterialMap().getMaterialIdAtIdx(i))
                                                                  for i in range(sliced.getMaterialMap().size())]}
    output_path.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    print(json.dumps({"versions": result["versions"], "dimension": project["dimension"],
                      "points": len(initial["points"]), "slice": "slice" in result}))


if __name__ == "__main__":
    main()
