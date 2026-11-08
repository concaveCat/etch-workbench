"""Read a selected GDS structure with the installed ViennaPS bindings.

This worker is intentionally launched only under scripts/bounded_child.py.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.gds import add_periodic_extent_support, parse_gds


def main() -> None:
    config_path, output_path = (Path(item).resolve() for item in sys.argv[1:3])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    source = Path(config["sourcePath"]).resolve()
    asset_id = str(config["assetId"])
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != asset_id:
        raise RuntimeError("GDS source does not match the requested SHA-256")
    library = parse_gds(raw)
    selected_layers = [int(value) for value in config["selectedLayers"]]
    selected_layer_datatypes = config.get("selectedLayerDatatypes")
    native_um = library.meters_per_database_unit / library.user_units_per_database_unit * 1_000_000
    effective_scale = native_um * float(config["unitScale"])
    selected, transform = library.select_and_transform(
        config["structure"], unit_scale=effective_scale,
        position_um=config["position"], selected_layers=selected_layers,
        selected_layer_datatypes=selected_layer_datatypes)
    filtered_path = output_path.parent / "selected.gds"
    filtered_path.write_bytes(selected)

    import viennaps as ps

    dimension = int(config["dimension"])
    delta = float(config["gridDelta"])
    boundary = ps.BoundaryType.REFLECTIVE_BOUNDARY if config["boundary"] == "reflective" else ps.BoundaryType.PERIODIC_BOUNDARY
    ps.setDimension(dimension)
    periodic_support = {}
    if config["boundary"] == "periodic" and config.get("xExtent") is not None:
        probe_path = output_path.parent / "periodic-bounds-probe.gds"
        probe_path.write_bytes(selected)
        if dimension == 2:
            probe = ps.d2.GDSGeometry(delta)
            probe.setBoundaryConditions([boundary, ps.BoundaryType.INFINITE_BOUNDARY])
            ps.d2.GDSReader(probe, str(probe_path)).apply()
            bounds = [float(value) for value in probe.getBounds()]
            if (bounds[0] < -float(config["xExtent"]) / 2.0 - delta * 1e-6
                    or bounds[1] > float(config["xExtent"]) / 2.0 + delta * 1e-6):
                raise RuntimeError("periodic GDS geometry extends outside the requested x domain")
            selected, periodic_support = add_periodic_extent_support(
                selected, config["structure"], dimension=2,
                x_extent_um=float(config["xExtent"]),
                cross_section_y_bounds_um=(bounds[2], bounds[3]), grid_delta_um=delta)
        else:
            probe = ps.d3.GDSGeometry(delta)
            probe.setBoundaryConditions([boundary, boundary, ps.BoundaryType.INFINITE_BOUNDARY])
            ps.d3.GDSReader(probe, str(probe_path)).apply()
            bounds = [float(value) for value in probe.getBounds()]
            y_extent = config.get("yExtent")
            if y_extent is None:
                raise RuntimeError("3D periodic GDS inspection requires the requested y extent")
            if (bounds[0] < -float(config["xExtent"]) / 2.0 - delta * 1e-6
                    or bounds[1] > float(config["xExtent"]) / 2.0 + delta * 1e-6
                    or bounds[2] < -float(y_extent) / 2.0 - delta * 1e-6
                    or bounds[3] > float(y_extent) / 2.0 + delta * 1e-6):
                raise RuntimeError("periodic GDS geometry extends outside the requested XY domain")
            selected, periodic_support = add_periodic_extent_support(
                selected, config["structure"], dimension=3,
                x_extent_um=float(config["xExtent"]), y_extent_um=float(y_extent),
                grid_delta_um=delta)
        del probe
        probe_path.unlink(missing_ok=True)
        filtered_path.write_bytes(selected)
    if dimension == 2:
        geometry = ps.d2.GDSGeometry(delta)
        geometry.setBoundaryConditions([boundary, ps.BoundaryType.INFINITE_BOUNDARY])
        ps.d2.GDSReader(geometry, str(filtered_path)).apply()
        native_bounds_raw = [float(value) for value in geometry.getBounds()]
        native_bounds = [value if math.isfinite(value) else None for value in native_bounds_raw]
        tolerance = max(1e-7, float(periodic_support.get("coordinateToleranceUm", 0.0)) * 1.1)
        if periodic_support and (abs(native_bounds_raw[0] + float(config["xExtent"]) / 2.0) > tolerance
                                 or abs(native_bounds_raw[1] - float(config["xExtent"]) / 2.0) > tolerance):
            raise RuntimeError("native periodic 2D GDS bounds do not match the requested x period")
        native_layers = sorted(int(value) for value in geometry.getAllLayers())
        level_sets = {str(layer): int(geometry.layerToLevelSet(layer, True).getNumberOfPoints()) for layer in selected_layers}
    else:
        geometry = ps.d3.GDSGeometry(delta)
        geometry.setBoundaryConditions([boundary, boundary, ps.BoundaryType.INFINITE_BOUNDARY])
        ps.d3.GDSReader(geometry, str(filtered_path)).apply()
        native_bounds_raw = [float(value) for value in geometry.getBounds()]
        native_bounds = [value if math.isfinite(value) else None for value in native_bounds_raw]
        tolerance = max(1e-7, float(periodic_support.get("coordinateToleranceUm", 0.0)) * 1.1)
        if periodic_support and (abs(native_bounds_raw[0] + float(config["xExtent"]) / 2.0) > tolerance
                                 or abs(native_bounds_raw[1] - float(config["xExtent"]) / 2.0) > tolerance
                                 or abs(native_bounds_raw[2] + float(config["yExtent"]) / 2.0) > tolerance
                                 or abs(native_bounds_raw[3] - float(config["yExtent"]) / 2.0) > tolerance):
            raise RuntimeError("native periodic 3D GDS bounds do not match the requested XY periods")
        native_layers = sorted(int(value) for value in geometry.getAllLayers())
        level_sets = {}
        for layer in selected_layers:
            level_set = geometry.layerToLevelSet(layer, 0.0, float(config.get("probeHeight", 1.0)), False, True)
            level_sets[str(layer)] = int(level_set.getNumberOfPoints())
    selected_native_layers = sorted(set(native_layers) & set(selected_layers))
    if selected_native_layers != sorted(selected_layers):
        raise RuntimeError(f"native GDS layer readback {selected_native_layers} differs from selected layers {sorted(selected_layers)}")
    support_layers = sorted(set(native_layers) - set(selected_layers))
    report = {
        "schemaVersion": 1,
        "versions": {"viennaps": version("viennaps"), "viennals": version("viennals")},
        "sourceAssetId": asset_id,
        "selectedStructure": config["structure"],
        "includedStructures": transform["includedStructures"],
        "selectedLayers": selected_layers,
        "nativeLayers": native_layers,
        "selectedNativeLayers": selected_native_layers,
        "supportLayers": support_layers,
        "nativeStructureCount": int(geometry.getNumberOfStructures()),
        "nativeBounds": native_bounds,
        **periodic_support,
        "gridDelta": delta,
        "dimension": dimension,
        "nativeUserUnitUm": native_um,
        "userUnitScaleAdjustment": float(config["unitScale"]),
        "levelSetPointCounts": level_sets,
        "filteredFileSha256": hashlib.sha256(selected).hexdigest(),
        "coordinateMapping": "GDS XY -> Workbench x,h in 2D; GDS XY -> x,y and mapped layer heights -> z in 3D",
        "transform": transform,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "nativeLayers": native_layers,
                      "nativeBounds": report["nativeBounds"], "levelSetPointCounts": level_sets}, ensure_ascii=False))


if __name__ == "__main__":
    main()
