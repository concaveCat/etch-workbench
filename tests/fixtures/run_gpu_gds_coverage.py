"""Bounded product/direct GPU_DISK verification for uploaded GDS geometry."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from run_gpu_geometry_coverage import (
    PYTHON, ROOT, bounded, configure_species_profile, direct_signature, make_project, mesh_signature,
)

OUT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-geometry"
RUN_ROOT = ROOT / "var/gpu-gds-coverage"
GDS_SOURCE = ROOT / "test-suite-gpu-data/gds-assets/7b7852d2e6a9ee0cd977d5e82dd95ee3d4c3a6fd0f9cf0d6ff0894eee1676c2d.gds"
ASSET_ID = hashlib.sha256(GDS_SOURCE.read_bytes()).hexdigest()
DIRECT = ROOT / "tests/fixtures/gpu_geometry_direct_reference.py"
STAGE5 = ROOT / "docs/stage5-evidence"


def gds_geometry(dimension: int, masked: bool,
                 layer_material: str | None = None, *, asset_id: str | None = None,
                 structure: str | None = None,
                 layer_mappings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    input_name = "gds-2d-CELL_A-direct-input.json" if dimension == 2 else "gds-3d-CELL_B-direct-input.json"
    reference = json.loads((STAGE5 / input_name).read_text(encoding="utf-8"))
    geometry = reference["project"]["geometry"]
    geometry["assetId"] = asset_id or ASSET_ID
    if structure:
        geometry["structure"] = structure
    if layer_material:
        geometry["layers"][0]["material"] = layer_material
    if layer_mappings is not None:
        geometry["layers"] = json.loads(json.dumps(layer_mappings))
    if dimension == 3 and layer_mappings is None:
        geometry["layers"][0]["mask"] = masked
    return geometry


def project_for(dimension: int, model: str, operation: str, boundary: str,
                masked: bool, layer_material: str | None = None,
                deposit_material: str | None = None,
                custom_definitions: list[dict[str, Any]] | None = None,
                material_tables: bool = False, *, asset_id: str | None = None,
                structure: str | None = None,
                layer_mappings: list[dict[str, Any]] | None = None,
                position_um: list[float] | None = None,
                domain_x_extent_um: float | None = None,
                geometry_unit_scale: float | None = None,
                domain_grid_delta_um: float | None = None,
                species_profile: str | None = None) -> dict[str, Any]:
    project = make_project("plane", dimension, False, model, operation)
    reference_name = "gds-2d-CELL_A-direct-input.json" if dimension == 2 else "gds-3d-CELL_B-direct-input.json"
    reference = json.loads((STAGE5 / reference_name).read_text(encoding="utf-8"))["project"]
    project["domain"] = reference["domain"]
    if domain_grid_delta_um is not None:
        project["domain"]["gridDelta"] = float(domain_grid_delta_um)
    if domain_x_extent_um is not None:
        project["domain"]["xExtent"] = float(domain_x_extent_um)
    if dimension == 2:
        project["domain"]["xBoundary"] = boundary
        project["view"] = reference["view"]
    else:
        project["domain"]["boundaryXY"] = boundary
        project["view3D"]["visibleMaterialIds"] = [30, 70]
        project["view3D"]["opacityByMaterial"] = {"30": 1.0, "70": 1.0}
    project["geometry"] = gds_geometry(dimension, masked, layer_material, asset_id=asset_id,
                                        structure=structure, layer_mappings=layer_mappings)
    if geometry_unit_scale is not None:
        project["geometry"]["unitScale"] = float(geometry_unit_scale)
    if position_um is not None:
        project["geometry"]["position"] = [float(value) for value in position_um]
    project["solver"]["enginePreference"] = "gpu"
    project["solver"]["random"] = {"mode": "fixed", "seed": 314159}
    project["solver"]["raysPerPoint"] = 32
    project["solver"]["rayTracing"].update({"maxBoundaryHits": 80, "maxReflections": 3})
    project["steps"][0]["id"] = "gpu_gds_process"
    project["steps"][0]["name"] = f"GDS {dimension}D {model} {operation}"
    configure_species_profile(project["steps"][0], species_profile)
    visible_uuid = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",", 1)[0].strip()
    if visible_uuid.startswith("GPU-"):
        project["solver"]["gpuDeviceId"] = visible_uuid
    if operation == "deposit":
        project["steps"][0]["depositMaterial"] = deposit_material or ("Si3N4" if dimension == 2 else "SiO2")
    if custom_definitions:
        project["materialDefinitions"] = custom_definitions
        if dimension == 3:
            used_materials = {layer["material"] for layer in project["geometry"]["layers"]}
            if project["steps"][0].get("depositMaterial"):
                used_materials.add(project["steps"][0]["depositMaterial"])
            for definition in custom_definitions:
                if definition["name"] not in used_materials:
                    continue
                custom_id = int(definition["id"])
                if custom_id not in project["view3D"]["visibleMaterialIds"]:
                    project["view3D"]["visibleMaterialIds"].append(custom_id)
                project["view3D"]["opacityByMaterial"][str(custom_id)] = 1.0
    if material_tables and model == "multiParticle":
        names = {row["material"] for row in project["geometry"]["layers"]}
        if project["steps"][0].get("depositMaterial"):
            names.add(project["steps"][0]["depositMaterial"])
        species = project["steps"][0]["species"]
        project["steps"][0]["rateCoefficients"]["byMaterial"] = {
            name: {
                item["label"]: round((0.03 + species_index * 0.001) if item["kind"] == "ion"
                                     else (0.04 + species_index * 0.002), 6)
                for species_index, item in enumerate(species)
            }
            for index, name in enumerate(sorted(names))
        }
        neutral = next(row for row in project["steps"][0]["species"] if row["kind"] == "neutral")
        neutral["stickingProbabilityByMaterial"] = {
            name: round(0.25 + 0.5 * index / max(len(names) - 1, 1), 6)
            for index, name in enumerate(sorted(names))
        }
    return project


def case_name(project: dict[str, Any], model: str, operation: str, boundary: str,
              masked: bool, label: str | None = None) -> str:
    dimension = project["dimension"]
    mask_tag = "-masked" if masked and dimension == 3 else ""
    label_tag = f"-{label}" if label else ""
    return f"{model}-{dimension}d-gds-{project['geometry']['structure']}{mask_tag}{label_tag}-{boundary}-{operation}-gpu_disk"


def run_case(dimension: int, model: str, operation: str, boundary: str, masked: bool,
             *, layer_material: str | None = None, deposit_material: str | None = None,
             custom_definitions: list[dict[str, Any]] | None = None,
             material_tables: bool = False, label: str | None = None,
             evidence_root: Path = OUT, asset_id: str | None = None,
             structure: str | None = None,
             layer_mappings: list[dict[str, Any]] | None = None,
             position_um: list[float] | None = None,
             domain_x_extent_um: float | None = None,
             geometry_unit_scale: float | None = None,
             domain_grid_delta_um: float | None = None,
             species_profile: str | None = None) -> dict[str, Any]:
    project = project_for(dimension, model, operation, boundary, masked, layer_material,
                          deposit_material, custom_definitions, material_tables,
                          asset_id=asset_id, structure=structure, layer_mappings=layer_mappings,
                          position_um=position_um, domain_x_extent_um=domain_x_extent_um,
                          geometry_unit_scale=geometry_unit_scale,
                          domain_grid_delta_um=domain_grid_delta_um,
                          species_profile=species_profile)
    name = case_name(project, model, operation, boundary, masked, label)
    evidence = evidence_root / name
    evidence.mkdir(parents=True, exist_ok=True)
    project_path = evidence / "project.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n")
    run_id = hashlib.sha256(name.encode()).hexdigest()[:24]
    run_dir = RUN_ROOT / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    gds_copy = run_dir / "gds-source.gds"
    shutil.copyfile(GDS_SOURCE, gds_copy)
    if hashlib.sha256(gds_copy.read_bytes()).hexdigest() != project["geometry"]["assetId"]:
        raise RuntimeError("GDS run copy hash differs from project asset ID")
    config_path = run_dir / "input.json"
    config = {
        "kind": "simulate", "runId": run_id, "project": project,
        "runSeed": project["solver"]["random"]["seed"],
        "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                              "viennals": importlib.metadata.version("viennals")},
        "stepEngines": {project["steps"][0]["id"]: "GPU_DISK"},
        "customMaterials": custom_definitions or [], "gdsAssetPath": str(gds_copy),
    }
    gpu_multi_particle = model == "multiParticle"
    gpu_multi_ion = (gpu_multi_particle and sum(
        item.get("kind") == "ion" for item in project["steps"][0].get("species", [])
    ) > 1)
    runtime_root = None
    runtime_fingerprint = None
    if gpu_multi_particle:
        from backend.native_build import configured_runtime

        runtime_root, runtime_fingerprint = configured_runtime()
        config["nativeEngineFingerprint"] = runtime_fingerprint
    if gpu_multi_ion:
        config["stepExecutionStrategies"] = {project["steps"][0]["id"]: "native-multi-ion-v1"}
    if project["solver"].get("gpuDeviceId"):
        config["selectedGpuDevice"] = {"uuid": project["solver"]["gpuDeviceId"]}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    product = bounded(name, "product", [str(PYTHON), str(ROOT / "worker/simulate.py"),
                                          str(config_path), str(run_dir)], evidence)
    direct_path = evidence / "direct.json"
    direct = bounded(name, "direct", [str(PYTHON), str(DIRECT), str(project_path),
                                       str(direct_path), str(run_dir / "direct-scratch"),
                                       "GPU_DISK", str(gds_copy)], evidence)
    row: dict[str, Any] = {
        "name": name, "dimension": dimension, "model": model, "operation": operation,
        "boundaryXY": boundary, "geometry": "gds", "structure": project["geometry"]["structure"],
        "gdsLayerMappings": project["geometry"]["layers"], "engine": "GPU_DISK",
        "layerMaterial": project["geometry"]["layers"][0]["material"],
        "depositMaterial": project["steps"][0].get("depositMaterial"),
        "customMaterialIds": sorted(int(definition["id"]) for definition in (custom_definitions or [])
                                     if definition["name"] in {
                                         *(layer["material"] for layer in project["geometry"]["layers"]),
                                         *(name for name in [project["steps"][0].get("depositMaterial")] if name),
                                     }),
        "materialTables": material_tables,
        "status": "failed", "productReturnCode": product.returncode,
        "directReturnCode": direct.returncode,
        "project": str(project_path.relative_to(ROOT)),
        "productGuard": str((evidence / "product.guard.json").relative_to(ROOT)),
        "directGuard": str((evidence / "direct.guard.json").relative_to(ROOT)),
        "productLog": str((evidence / "product.log").relative_to(ROOT)),
        "directLog": str((evidence / "direct.log").relative_to(ROOT)),
        "directReference": str(direct_path.relative_to(ROOT)),
    }
    if product.returncode == 0:
        worker = json.loads((run_dir / "worker-result.json").read_text(encoding="utf-8"))
        if gpu_multi_particle and worker.get("nativeEngineFingerprint") != runtime_fingerprint:
            raise RuntimeError("GDS worker did not load the configured patched ViennaPS runtime")
        first = worker["snapshots"][0]
        final_row = worker["snapshots"][-1]
        initial = json.loads((run_dir / first["snapshot"]).read_text(encoding="utf-8"))
        final = json.loads((run_dir / final_row["snapshot"]).read_text(encoding="utf-8"))
        shutil.copy2(run_dir / "worker-result.json", evidence / "worker-result.json")
        shutil.copy2(run_dir / config_path.name, evidence / "worker-input.json")
        shutil.copy2(run_dir / first["snapshot"], evidence / "initial.json")
        shutil.copy2(run_dir / final_row["snapshot"], evidence / "final.json")
        row["productActualEngine"] = worker["stepRecords"][0]["actualEngine"]
        row["productVersions"] = worker["versions"]
        row["productGeometryReadback"] = first.get("geometryReadback", {})
        row["productCallbacks"] = worker["stepRecords"][0].get("actualParameters", {}).get("rateCallbackSummary")
    else:
        row["productFailure"] = (product.stderr or product.stdout)[-2500:]
        initial = final = worker = None
    if direct.returncode == 0:
        reference = json.loads(direct_path.read_text(encoding="utf-8"))
        if gpu_multi_particle and not Path(reference["viennapsModulePath"]).resolve().is_relative_to(runtime_root / "viennaps"):
            raise RuntimeError("GDS direct reference did not load the configured patched ViennaPS runtime")
        row["directActualEngine"] = reference["actualEngine"]
        row["directVersions"] = reference["versions"]
        row["directGeometryReadback"] = reference.get("geometryReadback", {})
    else:
        row["directFailure"] = (direct.stderr or direct.stdout)[-2500:]
        reference = None
    if initial is not None and reference is not None:
        comparisons = {
            "initialPoints": mesh_signature(initial, dimension)["points"] == direct_signature(reference["initial"])["points"],
            "initialCells": mesh_signature(initial, dimension)["cells"] == direct_signature(reference["initial"])["cells"],
            "initialMaterialIds": mesh_signature(initial, dimension)["materialIds"] == direct_signature(reference["initial"])["materialIds"],
            "initialMaterialMap": mesh_signature(initial, dimension)["materialIdsByLevelSetIndex"] == direct_signature(reference["initial"])["materialIdsByLevelSetIndex"],
            "finalPoints": mesh_signature(final, dimension)["points"] == direct_signature(reference["final"])["points"],
            "finalCells": mesh_signature(final, dimension)["cells"] == direct_signature(reference["final"])["cells"],
            "finalMaterialIds": mesh_signature(final, dimension)["materialIds"] == direct_signature(reference["final"])["materialIds"],
            "finalMaterialMap": mesh_signature(final, dimension)["materialIdsByLevelSetIndex"] == direct_signature(reference["final"])["materialIdsByLevelSetIndex"],
            "productEngine": row.get("productActualEngine") == "GPU_DISK",
            "directEngine": row.get("directActualEngine") == "GPU_DISK",
            "versions": row.get("productVersions") == {"viennaps": "4.6.2", "viennals": "5.8.5"} == row.get("directVersions"),
            "selectedLayers": row["productGeometryReadback"].get("selectedNativeLayers") == row["directGeometryReadback"].get("selectedNativeLayers"),
            "selectedStructure": row["productGeometryReadback"].get("selectedStructure") == row["directGeometryReadback"].get("selectedStructure"),
            "sourceAssetId": row["productGeometryReadback"].get("sourceAssetId") == row["directGeometryReadback"].get("sourceAssetId") == project["geometry"]["assetId"],
            "filteredGdsHash": row["productGeometryReadback"].get("filteredFileSha256") == row["directGeometryReadback"].get("selectedFileSha256"),
            "layerMappings": row["productGeometryReadback"].get("layerMappings") == row["directGeometryReadback"].get("layerMappings") == project["geometry"]["layers"],
        }
        if model == "multiParticle":
            direct_callbacks = reference.get("callback") or {}
            product_callbacks = row.get("productCallbacks") or {}
            comparisons["rateCallbackCount"] = product_callbacks.get("invocationCount") == direct_callbacks.get("count")
            comparisons["rateCallbackMaterials"] = product_callbacks.get("materials", {}) == direct_callbacks.get("materials", {})
            comparisons["rateCallbackFluxSums"] = product_callbacks.get("fluxSums") == direct_callbacks.get("fluxSums")
            comparisons["rateCallbackFluxMaxima"] = product_callbacks.get("fluxMaxima") == direct_callbacks.get("fluxMaxima")
            comparisons["rateCallbackMeanFlux"] = product_callbacks.get("meanFluxBySpecies") == direct_callbacks.get("meanFluxBySpecies")
            product_samples = product_callbacks.get("samples") or []
            direct_samples = direct_callbacks.get("samples") or []
            compared_samples = min(len(product_samples), len(direct_samples))
            row["callbackSampleCounts"] = {"product": len(product_samples),
                                            "direct": len(direct_samples),
                                            "comparedPrefix": compared_samples}
            comparisons["rateCallbackFormulaSamples"] = (
                compared_samples == len(direct_samples) > 0
                and product_samples[:compared_samples] == direct_samples[:compared_samples]
            )
        row["comparisons"] = comparisons
        row["status"] = "verified" if all(comparisons.values()) else "mismatch"
        row["counts"] = {"initialPoints": len(initial["points"]), "finalPoints": len(final["points"]),
                         "initialCells": len(mesh_signature(initial, dimension)["cells"]),
                         "finalCells": len(mesh_signature(final, dimension)["cells"]),
                         "initialMaterialMap": initial["materialIdsByLevelSetIndex"],
                         "finalMaterialMap": final["materialIdsByLevelSetIndex"]}
    acceptance = evidence / "acceptance.json"
    acceptance.write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"name": name, "status": row["status"],
                      "comparisons": row.get("comparisons")}, ensure_ascii=False), flush=True)
    return row


def safe_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "material"


def custom_definitions_for(native_id: int) -> list[dict[str, Any]]:
    count = native_id - 177 + 1
    return [{"name": f"GdsGpuCustom{index:02d}", "id": 177 + index, "customId": index,
             "color": "#7755aa", "conductive": False,
             "description": "bounded GDS GPU custom material scope probe"}
            for index in range(count)]


def run_multiion_scope() -> int:
    evidence_root = OUT / "multi-ion"
    evidence_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for boundary in ("reflective", "periodic"):
        for dimension in (2, 3):
            masks = (False,) if dimension == 2 else (False, True)
            for masked in masks:
                for operation in ("etch", "deposit"):
                    # CELL_A's native layout bounds span x=0.25..7.5 um at
                    # the selected scale. Keep its configured placement and
                    # give the periodic domain enough width to contain the
                    # whole uploaded layout. Translating only CELL_A leaves
                    # other retained support-layer cells at their source
                    # coordinates, so a centered offset is not a valid way
                    # to bring the native layout bounds into this period.
                    periodic_2d_extent = 16.0 if dimension == 2 and boundary == "periodic" else None
                    label = "expanded-period-ion-neutral-ion" if periodic_2d_extent else "ion-neutral-ion"
                    rows.append(run_case(
                        dimension, "multiParticle", operation, boundary, masked,
                        material_tables=True, label=label, evidence_root=evidence_root,
                        domain_x_extent_um=periodic_2d_extent,
                        species_profile="ion-neutral-ion",
                    ))
    summary = {
        "schemaVersion": 1,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK", "installedVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "expectedCaseCount": 12, "caseCount": len(rows), "rows": rows,
        "passed": len(rows) == 12 and all(row["status"] == "verified" for row in rows),
        "scope": "GDS ion-neutral-ion MultiParticle across 2D/3D, film/mask, etch/deposit, reflective/periodic boundaries, and material-specific rate/sticking tables",
    }
    output = evidence_root / "summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(output.relative_to(ROOT)), "passed": summary["passed"],
                      "caseCount": len(rows), "expectedCaseCount": 12}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


def run_material_scope() -> int:
    catalog = json.loads((ROOT / "docs/materials-catalog.json").read_text(encoding="utf-8"))
    materials = [row["name"] for row in catalog["materials"]]
    scope_root = OUT / "material-scope"
    scope_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for boundary in ("reflective", "periodic"):
        for layer_material in materials:
            for dimension, masked, role in ((2, False, "film"), (3, False, "film"), (3, True, "mask")):
                label = f"{role}-{safe_tag(layer_material)}"
                for model in ("singleParticle", "multiParticle"):
                    rows.append(run_case(dimension, model, "etch", boundary, masked,
                                         layer_material=layer_material,
                                         material_tables=model == "multiParticle",
                                         label=label, evidence_root=scope_root))
        for deposit_material in materials:
            for dimension in (2, 3):
                for model in ("singleParticle", "multiParticle"):
                    label = f"deposit-{safe_tag(deposit_material)}"
                    rows.append(run_case(dimension, model, "deposit", boundary, False,
                                         deposit_material=deposit_material,
                                         material_tables=model == "multiParticle",
                                         label=label, evidence_root=scope_root))
        for native_id in (177, 240):
            custom_definitions = custom_definitions_for(native_id)
            custom_name = custom_definitions[-1]["name"]
            for dimension, masked, role in ((2, False, "film"), (3, False, "film"), (3, True, "mask")):
                label = f"custom-{native_id}-{role}"
                for model in ("singleParticle", "multiParticle"):
                    rows.append(run_case(dimension, model, "etch", boundary, masked,
                                         layer_material=custom_name,
                                         custom_definitions=custom_definitions,
                                         material_tables=model == "multiParticle",
                                         label=label, evidence_root=scope_root))
            for dimension in (2, 3):
                for model in ("singleParticle", "multiParticle"):
                    label = f"custom-{native_id}-deposit"
                    rows.append(run_case(dimension, model, "deposit", boundary, False,
                                         deposit_material=custom_name,
                                         custom_definitions=custom_definitions,
                                         material_tables=model == "multiParticle",
                                         label=label, evidence_root=scope_root))
    expected = 2 * (len(materials) * 3 * 2 + len(materials) * 2 * 2 + 2 * (3 * 2 + 2 * 2))
    summary = {
        "schemaVersion": 1,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK", "installedVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "builtInMaterials": materials, "customNativeIdEdges": [177, 240],
        "expectedCaseCount": expected, "caseCount": len(rows), "rows": rows,
        "passed": len(rows) == expected and all(row["status"] == "verified" for row in rows),
        "scope": "GDS single-layer film/mask native material identities and deposit bindings, both boundaries, SP/MP etch/deposit; MultiParticle material tables directly compared",
    }
    output = scope_root / "summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": str(output.relative_to(ROOT)), "passed": summary["passed"],
                      "caseCount": len(rows), "expectedCaseCount": expected}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("baseline", "material-scope", "multi-ion"), default="baseline")
    args = parser.parse_args()
    if args.phase == "material-scope":
        return run_material_scope()
    if args.phase == "multi-ion":
        return run_multiion_scope()
    OUT.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for boundary in ("reflective", "periodic"):
        for dimension in (2, 3):
            masks = (False,) if dimension == 2 else (False, True)
            for masked in masks:
                for model in ("singleParticle", "multiParticle"):
                    for operation in ("etch", "deposit"):
                        rows.append(run_case(dimension, model, operation, boundary, masked))
    summary = {
        "schemaVersion": 1,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK", "installedVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "expectedCaseCount": 24, "caseCount": len(rows), "rows": rows,
        "passed": len(rows) == 24 and all(row["status"] == "verified" for row in rows),
        "scope": "2D CELL_A and 3D CELL_B GDS geometry, reflective/periodic boundaries, mapped film/mask roles, SP/MP etch/deposit, product/direct exact mesh comparison",
    }
    output = OUT / "summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": str(output.relative_to(ROOT)), "passed": summary["passed"],
                      "caseCount": len(rows), "expectedCaseCount": 24}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
