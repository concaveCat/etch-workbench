"""Bounded product/direct GPU_DISK checks for installed specialized models on native GDS geometry."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from backend.specialized_models import FLUOROCARBON_MATERIAL_DEFAULTS, specialized_catalog
from run_gpu_gds_coverage import PYTHON, bounded, direct_signature, mesh_signature, project_for
from stage8_project_factory import make_project as make_specialized_project

os.environ.setdefault("PYTHONFAULTHANDLER", "1")

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-specialized"
RUN_ROOT = ROOT / "var/gpu-gds-specialized-coverage"
DIRECT = ROOT / "tests/fixtures/gpu_specialized_geometry_direct_reference.py"
SOURCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gds-multilayer/multilayer-two-adjacent-polygons.gds"
SINGLE_SOURCE = ROOT / "test-suite-gpu-data/gds-assets/7b7852d2e6a9ee0cd977d5e82dd95ee3d4c3a6fd0f9cf0d6ff0894eee1676c2d.gds"
VERSIONS = {"viennaps": "4.6.2", "viennals": "5.8.5"}
MODELS = tuple(spec["id"] for spec in specialized_catalog()["models"] if spec["usesFlux"])


def project_for_specialized(model: str, dimension: int, boundary: str,
                            profile: str) -> tuple[dict[str, Any], Path]:
    spec_project = make_specialized_project(model, dimension)
    if profile in {"single-layer", "single-layer-offset", "single-mask"}:
        source = SINGLE_SOURCE
        structure = "CELL_A" if dimension == 2 else "CELL_B"
        mappings = ([{"layer": 1, "material": "Si"}] if dimension == 2 else
                    [{"layer": 2, "material": "Mask" if profile == "single-mask" else "Si",
                      "baseHeight": 0.25, "height": 1.0, "mask": profile == "single-mask"}])
        position = ([0.25, 0.1, 0.0] if dimension == 2 else
                    [0.25, 0.25, 0.1] if profile == "single-layer-offset" else
                    [-0.25, 0.25, 0.1] if profile == "single-layer" else
                    [0.25, 0.25, 0.1])
        unit_scale = 1.5 if dimension == 2 else 1.0
    else:
        source = SOURCE
        structure = "MULTI"
        mask_material = "Si3N4" if model in {"teosDeposition", "teospecvd"} else "SiO2"
        mappings = ([{"layer": 3, "material": "Si"}, {"layer": 4, "material": "SiO2"}]
                    if dimension == 2 else
                    [{"layer": 3, "material": "Si", "baseHeight": 0.0, "height": 0.75, "mask": False},
                     {"layer": 4, "material": mask_material, "baseHeight": 1.0, "height": 0.5, "mask": True}])
        position = [0.25, 0.25, 0.0] if dimension == 2 else [0.25, 0.25, 0.25]
        unit_scale = 1.0
    shell = project_for(dimension, "singleParticle", spec_project["steps"][0]["operation"], boundary,
                        masked=dimension == 3 and any(row.get("mask", False) for row in mappings),
                        layer_material=mappings[0]["material"],
                        asset_id=hashlib.sha256(source.read_bytes()).hexdigest(), structure=structure,
                        layer_mappings=mappings, position_um=position,
                        domain_x_extent_um=(16.0 if dimension == 2 and profile.startswith("single") else 12.0),
                        geometry_unit_scale=unit_scale, domain_grid_delta_um=0.25)
    project = spec_project
    project["name"] = f"GDS specialized {model} {dimension}D {boundary}"
    project["domain"] = shell["domain"]
    project["geometry"] = shell["geometry"]
    if dimension == 2:
        project["view"] = shell["view"]
    else:
        project["view3D"] = shell["view3D"]
        project["view3D"]["visibleMaterialIds"] = [0, 10, 30]
    project["solver"]["enginePreference"] = "gpu"
    project["solver"]["random"] = {"mode": "fixed", "seed": 424242}
    project["solver"]["raysPerPoint"] = 32
    project["solver"]["rayTracing"].update({"maxBoundaryHits": 80, "maxReflections": 3})
    step = project["steps"][0]
    step["id"] = "gpu_gds_specialized_process"
    step["name"] = f"GDS {model}"
    if model == "fluorocarbon":
        geometry_materials = {str(row["material"]) for row in mappings}
        step["nativeParameters"]["materialParametersByMaterial"] = {
            name: dict(FLUOROCARBON_MATERIAL_DEFAULTS)
            for name in sorted(geometry_materials | {"Mask", "Polymer"})
        }
    if model == "neutralTransport":
        step["nativeParameters"]["etchFrontMaterial"] = mappings[0]["material"]
    return project, source


def close_points(left: list[list[float]], right: list[list[float]], tolerance: float = 1e-12) -> tuple[bool, float | None]:
    if len(left) != len(right) or any(len(a) != len(b) for a, b in zip(left, right)):
        return False, None
    delta = max((abs(float(a) - float(b)) for pa, pb in zip(left, right) for a, b in zip(pa, pb)), default=0.0)
    return delta <= tolerance, delta


def run_case(model: str, dimension: int, boundary: str, engine: str, profile: str) -> dict[str, Any]:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    project, source = project_for_specialized(model, dimension, boundary, profile)
    project["solver"]["enginePreference"] = "gpu" if engine == "GPU_DISK" else "cpu"
    name = f"{model}-{dimension}d-gds-{profile}-{boundary}-{engine.lower()}"
    evidence = EVIDENCE / name
    evidence.mkdir(parents=True, exist_ok=True)
    project_path = evidence / "project.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    run_id = hashlib.sha256(name.encode()).hexdigest()[:24]
    run_dir = RUN_ROOT / run_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    source_copy = run_dir / "gds-source.gds"
    shutil.copy2(source, source_copy)
    if hashlib.sha256(source_copy.read_bytes()).hexdigest() != project["geometry"]["assetId"]:
        raise RuntimeError("GDS asset copy does not match the project asset ID")
    config_path = run_dir / "input.json"
    config_path.write_text(json.dumps({
        "kind": "simulate", "runId": run_id, "project": project,
        "runSeed": project["solver"]["random"]["seed"], "expectedVersions": VERSIONS,
        "stepEngines": {project["steps"][0]["id"]: engine}, "customMaterials": [],
        "gdsAssetPath": str(source_copy),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    product = bounded(name, "product", [str(PYTHON), str(ROOT / "worker/simulate.py"), str(config_path), str(run_dir)], evidence)
    direct_path = evidence / "direct.json"
    direct_scratch = run_dir / "direct-scratch"
    direct = bounded(name, "direct", [str(PYTHON), str(DIRECT), str(project_path), str(direct_path),
                                      str(direct_scratch), engine, "--gds-source", str(source_copy)], evidence)
    row: dict[str, Any] = {
        "name": name, "status": "failed", "dimension": dimension, "geometry": "gds",
        "model": model, "nativeModel": model, "operation": project["steps"][0]["operation"],
        "boundaryXY": boundary, "engine": engine, "gdsLayerMappings": project["geometry"]["layers"],
        "project": str(project_path.relative_to(ROOT)), "directReference": str(direct_path.relative_to(ROOT)),
        "productGuard": str((evidence / "product.guard.json").relative_to(ROOT)),
        "directGuard": str((evidence / "direct.guard.json").relative_to(ROOT)),
        "productLog": str((evidence / "product.log").relative_to(ROOT)),
        "directLog": str((evidence / "direct.log").relative_to(ROOT)),
        "productReturnCode": product.returncode, "directReturnCode": direct.returncode,
        "versions": VERSIONS,
    }
    if product.returncode != 0:
        row["productFailure"] = (product.stderr or product.stdout)[-3000:]
    if direct.returncode != 0:
        row["directFailure"] = (direct.stderr or direct.stdout)[-3000:]

    worker_path = run_dir / "worker-result.json"
    if product.returncode == 0 and worker_path.is_file():
        worker = json.loads(worker_path.read_text(encoding="utf-8"))
        first, last = worker["snapshots"][0], worker["snapshots"][-1]
        initial = json.loads((run_dir / first["snapshot"]).read_text(encoding="utf-8"))
        final = json.loads((run_dir / last["snapshot"]).read_text(encoding="utf-8"))
        record = next(item for item in worker["stepRecords"] if item["stepId"] == project["steps"][0]["id"])
        row.update(productActualEngine=record["actualEngine"], productVersions=worker["versions"],
                   productGeometryReadback=first.get("geometryReadback", {}),
                   productActualParameters=record.get("actualParameters", {}))
        for src, name_out in ((worker_path, "worker-result.json"), (config_path, "worker-input.json"),
                              (run_dir / first["snapshot"], "initial.json"), (run_dir / last["snapshot"], "final.json"),
                              (run_dir / first["vtk"], "initial.vtk"), (run_dir / last["vtk"], "final.vtk")):
            shutil.copy2(src, evidence / name_out)
    else:
        worker = None
        initial = final = None
    if direct.returncode == 0 and direct_path.is_file():
        reference = json.loads(direct_path.read_text(encoding="utf-8"))
        row.update(directActualEngine=reference["actualEngine"], directVersions=reference["versions"],
                   directGeometryReadback=reference.get("geometryReadback", {}))
    else:
        reference = None
    if initial is not None and reference is not None:
        record = next(item for item in worker["stepRecords"] if item["stepId"] == project["steps"][0]["id"])
        comparisons: dict[str, bool] = {}
        deltas = {}
        for phase, product_mesh, direct_mesh in (("initial", initial, reference["initial"]),
                                                  ("final", final, reference["final"])):
            product_mesh_sig = mesh_signature(product_mesh, dimension)
            direct_mesh_sig = direct_signature(direct_mesh)
            points_match, delta = close_points(product_mesh_sig["points"], direct_mesh_sig["points"])
            comparisons[f"{phase}Points"] = points_match
            deltas[phase] = delta
            for suffix, key in (("Cells", "cells"), ("MaterialIds", "materialIds"),
                                ("MaterialMap", "materialIdsByLevelSetIndex")):
                comparisons[f"{phase}{suffix}"] = product_mesh_sig[{
                    "cells": "cells", "materialIds": "materialIds",
                    "materialIdsByLevelSetIndex": "materialIdsByLevelSetIndex",
                }[key]] == direct_mesh_sig[key]
        comparisons.update({
            "productEngine": row.get("productActualEngine") == engine,
            "directEngine": row.get("directActualEngine") == engine,
            "versions": row.get("productVersions") == VERSIONS == row.get("directVersions"),
            "selectedLayers": row["productGeometryReadback"].get("selectedNativeLayers") == row["directGeometryReadback"].get("selectedNativeLayers"),
            "selectedStructure": row["productGeometryReadback"].get("selectedStructure") == row["directGeometryReadback"].get("selectedStructure"),
            "sourceAssetId": row["productGeometryReadback"].get("sourceAssetId") == row["directGeometryReadback"].get("sourceAssetId") == project["geometry"]["assetId"],
            "layerMappings": row["productGeometryReadback"].get("layerMappings") == row["directGeometryReadback"].get("layerMappings") == project["geometry"]["layers"],
            "nativeModel": record.get("actualParameters", {}).get("nativeModel", {}).get("nativeModel") == model,
        })
        if boundary == "periodic":
            comparisons.update({
                "periodicSupportLayer": (row["productGeometryReadback"].get("supportLayer")
                                          == row["directGeometryReadback"].get("supportLayer")),
                "periodicSupportBounds": (row["productGeometryReadback"].get("supportBoundsUm")
                                          == row["directGeometryReadback"].get("supportBoundsUm")),
                "periodicNativeBounds": (row["productGeometryReadback"].get("nativeBounds")
                                         == row["directGeometryReadback"].get("nativeBounds")),
            })
        row["comparisons"] = comparisons
        row["coordinateTolerance"] = 1e-12
        row["coordinateDeltaMaxAbs"] = deltas
        row["status"] = "verified" if all(comparisons.values()) else "mismatch"
    elif product.returncode != 0:
        row["status"] = "product-failed"
    elif direct.returncode != 0:
        row["status"] = "direct-failed"
    (evidence / "acceptance.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: row.get(key) for key in ("name", "status", "comparisons", "productFailure", "directFailure") if key in row}, ensure_ascii=False), flush=True)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", choices=MODELS)
    parser.add_argument("--dimension", action="append", type=int, choices=(2, 3))
    parser.add_argument("--boundary", action="append", choices=("reflective", "periodic"))
    parser.add_argument("--engine", choices=("GPU_DISK", "CPU_DISK"), default="GPU_DISK")
    parser.add_argument("--profile", choices=("two-layer-film-mask", "single-layer", "single-layer-offset", "single-mask"),
                        default="two-layer-film-mask")
    args = parser.parse_args()
    models = args.model or list(MODELS)
    dimensions = args.dimension or [2, 3]
    boundaries = args.boundary or ["reflective", "periodic"]
    rows = [run_case(model, dimension, boundary, args.engine, args.profile)
            for model in models for dimension in dimensions for boundary in boundaries]
    result = {"schemaVersion": 1, "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "status": "passed" if rows and all(row["status"] == "verified" for row in rows) else "incomplete",
              "passed": bool(rows) and all(row["status"] == "verified" for row in rows),
              "engine": args.engine, "installedVersions": {
                  "viennaps": importlib.metadata.version("viennaps"),
                  "viennals": importlib.metadata.version("viennals")},
              "geometry": ("single native GDS polygon" if args.profile.startswith("single-layer") or args.profile == "single-mask" else
                           "synthetic MULTI structure with two adjacent GDS polygons; 2D film/film, 3D film/mask"),
              "scope": "installed specialized model × GDS profile × dimension × boundary; real product worker and independent direct ViennaPS references",
              "expectedCaseCount": len(models) * len(dimensions) * len(boundaries), "caseCount": len(rows), "rows": rows}
    summary_path = (EVIDENCE / "summary.json" if args.model is None and args.dimension is None
                    and args.boundary is None and args.engine == "GPU_DISK"
                    and args.profile == "two-layer-film-mask" else
                    EVIDENCE / f"targeted-summary-{args.engine.lower()}-{args.profile}.json")
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary_path.relative_to(ROOT)), "passed": result["passed"],
                      "caseCount": len(rows), "expectedCaseCount": result["expectedCaseCount"]}, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
