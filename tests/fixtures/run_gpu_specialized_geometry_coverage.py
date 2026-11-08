"""Bounded GPU_DISK cross-coverage for preset geometry × installed flux models."""
from __future__ import annotations

import hashlib
import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from backend.specialized_models import FLUOROCARBON_MATERIAL_DEFAULTS, specialized_catalog
from run_gpu_geometry_coverage import geometry_cases, geometry_input
from stage8_project_factory import make_project as make_specialized_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries"
RUN_ROOT = ROOT / "var/gpu-specialized-geometry-coverage"
DIRECT = ROOT / "tests/fixtures/gpu_specialized_geometry_direct_reference.py"
PYTHON = ROOT / ".venv/bin/python"
VERSIONS = {"viennaps": "4.6.2", "viennals": "5.8.5"}
GPU_MODELS = tuple(spec["id"] for spec in specialized_catalog()["models"] if spec["usesFlux"])


def process_materials(geometry: dict[str, Any], step: dict[str, Any]) -> set[str]:
    names = {str(geometry["substrateMaterial"])}
    if geometry["kind"] in {"trench", "hole", "fin"} and float(geometry.get("maskHeight", 0)) > 0:
        names.add(str(geometry.get("maskMaterial", "Mask")))
    if step.get("depositMaterial"):
        names.add(str(step["depositMaterial"]))
    return names


def bounded(name: str, kind: str, command: list[str], evidence: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        str(PYTHON), str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
        "--record", str(evidence / f"{kind}.guard.json"),
        "--log-path", str(evidence / f"{kind}.log"),
        "--timeout-seconds", "120", "--max-rss-mib", "1800",
        "--max-log-bytes", "2000000", "--terminate-grace-seconds", "2", "--",
        *command,
    ], cwd=ROOT, capture_output=True, text=True, timeout=132, check=False)


def product_signature(snapshot: dict[str, Any], dimension: int) -> dict[str, Any]:
    return {"points": snapshot["points"], "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: snapshot[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def point_comparison(a: list[list[float]], b: list[list[float]], tolerance: float = 1e-12) -> tuple[bool, float | None]:
    """Compare native coordinates tightly while tolerating floating-point roundoff."""
    if len(a) != len(b) or any(len(left) != len(right) for left, right in zip(a, b)):
        return False, None
    maximum = max((abs(float(x) - float(y)) for left, right in zip(a, b) for x, y in zip(left, right)), default=0.0)
    return maximum <= tolerance, maximum


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", action="append", choices=("plane", "trench", "hole", "fin"))
    parser.add_argument("--model", action="append", choices=GPU_MODELS)
    parser.add_argument("--dimension", action="append", type=int, choices=(2, 3))
    parser.add_argument("--fin-mode", choices=("direct", "extruded"))
    parser.add_argument("--fin-construction", choices=("native", "boolean"), default="native")
    parser.add_argument("--fin-auto-fallback", action="store_true",
                        help="keep native in saved input and verify the product/direct safe Boolean fallback")
    parser.add_argument("--mask-height", type=float)
    parser.add_argument("--boundary", choices=("reflective", "periodic"), default="reflective")
    parser.add_argument("--engine", choices=("GPU_DISK", "CPU_DISK"), default="GPU_DISK")
    parser.add_argument("--substrate-material", default="Si",
                        help="probe a registered native substrate material without changing model parameters")
    parser.add_argument("--rate-scale", type=float, default=1.0)
    parser.add_argument("--duration-scale", type=float, default=1.0)
    args = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    selected_geometries = [case for case in geometry_cases()
                           if (not args.geometry or case[0] in args.geometry)
                           and (not args.dimension or case[1] in args.dimension)
                           and (args.fin_mode is None or case[0] != "fin"
                                or case[2] == (args.fin_mode == "extruded"))]
    selected_models = tuple(args.model or GPU_MODELS)
    for kind, dimension, extruded in selected_geometries:
        for model in selected_models:
            suffix = "extruded" if extruded else "direct"
            substrate_tag = f"-bulk-{args.substrate_material}" if args.substrate_material != "Si" else ""
            mask_tag = f"-mask-{args.mask_height:g}" if kind in {"trench", "hole", "fin"} and args.mask_height is not None else ""
            construction_tag = ("-auto-fallback" if kind == "fin" and args.fin_auto_fallback else
                                "-boolean" if kind == "fin" and args.fin_construction == "boolean" else "")
            rate_tag = f"-rate-{args.rate_scale:g}-time-{args.duration_scale:g}" if args.rate_scale != 1.0 or args.duration_scale != 1.0 else ""
            boundary_tag = "-periodic" if args.boundary == "periodic" else ""
            name = f"{model}-{dimension}d-{kind}-{suffix}{construction_tag}{substrate_tag}{mask_tag}{rate_tag}{boundary_tag}-{args.engine.lower()}"
            evidence = EVIDENCE / name
            evidence.mkdir(parents=True, exist_ok=True)
            project = make_specialized_project(model, dimension)
            project["geometry"] = geometry_input(kind, dimension, extruded)
            project["geometry"]["substrateMaterial"] = args.substrate_material
            if dimension == 2:
                project["domain"]["xBoundary"] = args.boundary
            else:
                project["domain"]["boundaryXY"] = args.boundary
            if kind == "fin":
                project["geometry"]["construction"] = "native" if args.fin_auto_fallback else args.fin_construction
            if kind in {"trench", "hole", "fin"} and args.mask_height is not None:
                project["geometry"]["maskHeight"] = args.mask_height
            project["solver"]["enginePreference"] = "gpu" if args.engine.startswith("GPU_") else "cpu"
            step = project["steps"][0]
            step["id"] = "gpu_specialized_geometry_process"
            if model == "neutralTransport":
                # The WebUI validator requires the native etch front to be an
                # actual geometry material. Keep this bound material aligned
                # with the selected substrate in the sweep.
                step["nativeParameters"]["etchFrontMaterial"] = args.substrate_material
            if args.duration_scale != 1.0 and step.get("duration") is not None:
                step["duration"] *= args.duration_scale
            if args.rate_scale != 1.0:
                for key, value in list(step["nativeParameters"].items()):
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and (
                        "Flux" in key or "Rate" in key or key in {"planeWaferRate", "redepositionRate"}
                    ):
                        step["nativeParameters"][key] = float(value) * args.rate_scale
            if model == "fluorocarbon":
                names = process_materials(project["geometry"], step) | {"Mask", "Polymer"}
                step["nativeParameters"]["materialParametersByMaterial"] = {
                    name: dict(FLUOROCARBON_MATERIAL_DEFAULTS) for name in sorted(names)
                }
            project_path = evidence / "project.json"
            project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            digest = hashlib.sha256(name.encode()).hexdigest()[:24]
            run_dir = RUN_ROOT / digest
            if run_dir.exists(): shutil.rmtree(run_dir)
            run_dir.mkdir(parents=True, mode=0o700)
            config_path = run_dir / "input.json"
            config = {"kind": "simulate", "runId": digest, "project": project,
                      "runSeed": project["solver"]["random"]["seed"], "expectedVersions": VERSIONS,
                      "stepEngines": {step["id"]: args.engine}, "customMaterials": []}
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            product = bounded(name, "product", [str(PYTHON), str(ROOT / "worker/simulate.py"),
                              str(config_path), str(run_dir)], evidence)
            direct_path = evidence / "direct.json"
            direct = bounded(name, "direct", [str(PYTHON), str(DIRECT), str(project_path),
                            str(direct_path), str(run_dir / "direct-scratch"), args.engine], evidence)
            row: dict[str, Any] = {
                "name": name, "status": "failed", "dimension": dimension, "geometry": kind,
                "finExtrusionMode": ("extruded" if extruded else "direct") if kind == "fin" else None,
                "finConstruction": project["geometry"].get("construction") if kind == "fin" else None,
                "model": model, "nativeModel": model, "operation": step["operation"], "engine": args.engine,
                "project": str(project_path.relative_to(ROOT)),
                "productGuard": str((evidence / "product.guard.json").relative_to(ROOT)),
                "directGuard": str((evidence / "direct.guard.json").relative_to(ROOT)),
                "productLog": str((evidence / "product.log").relative_to(ROOT)),
                "directLog": str((evidence / "direct.log").relative_to(ROOT)),
                "directReference": str(direct_path.relative_to(ROOT)),
                "productReturnCode": product.returncode, "directReturnCode": direct.returncode,
            }
            if product.returncode != 0:
                row["productFailure"] = (product.stderr or product.stdout)[-2500:]
            if direct.returncode != 0:
                row["directFailure"] = (direct.stderr or direct.stdout)[-2500:]
            worker_path = run_dir / "worker-result.json"
            if product.returncode == 0 and worker_path.is_file():
                worker = json.loads(worker_path.read_text(encoding="utf-8"))
                initial_meta, final_meta = worker["snapshots"][0], worker["snapshots"][-1]
                initial = json.loads((run_dir / initial_meta["snapshot"]).read_text(encoding="utf-8"))
                final = json.loads((run_dir / final_meta["snapshot"]).read_text(encoding="utf-8"))
                record = next(item for item in worker["stepRecords"] if item.get("stepId") == step["id"])
                row.update(productActualEngine=record["actualEngine"], productVersions=worker["versions"],
                           productActualParameters=record.get("actualParameters", {}),
                           productStepId=record["stepId"])
                for src, dst in ((worker_path, evidence / "worker-result.json"),
                                 (config_path, evidence / "worker-input.json"),
                                 (run_dir / initial_meta["snapshot"], evidence / "initial.json"),
                                 (run_dir / final_meta["snapshot"], evidence / "final.json"),
                                 (run_dir / initial_meta["vtk"], evidence / "initial.vtk"),
                                 (run_dir / final_meta["vtk"], evidence / "final.vtk")):
                    shutil.copy2(src, dst)
            else:
                initial = final = None
            if direct.returncode == 0 and direct_path.is_file():
                reference = json.loads(direct_path.read_text(encoding="utf-8"))
                row.update(directActualEngine=reference.get("actualEngine"), directVersions=reference.get("versions"))
            else:
                reference = None
            if initial is not None and reference is not None:
                product_geometry = initial_meta.get("geometryReadback", {})
                direct_geometry = reference.get("finConstruction", {})
                initial_points_match, initial_point_delta = point_comparison(
                    product_signature(initial, dimension)["points"], direct_signature(reference["initial"])["points"])
                final_points_match, final_point_delta = point_comparison(
                    product_signature(final, dimension)["points"], direct_signature(reference["final"])["points"])
                comparisons = {
                    "initialPoints": initial_points_match,
                    "initialCells": product_signature(initial, dimension)["cells"] == direct_signature(reference["initial"])["cells"],
                    "initialMaterialIds": product_signature(initial, dimension)["materialIds"] == direct_signature(reference["initial"])["materialIds"],
                    "initialMaterialMap": product_signature(initial, dimension)["materialIdsByLevelSetIndex"] == direct_signature(reference["initial"])["materialIdsByLevelSetIndex"],
                    "finalPoints": final_points_match,
                    "finalCells": product_signature(final, dimension)["cells"] == direct_signature(reference["final"])["cells"],
                    "finalMaterialIds": product_signature(final, dimension)["materialIds"] == direct_signature(reference["final"])["materialIds"],
                    "finalMaterialMap": product_signature(final, dimension)["materialIdsByLevelSetIndex"] == direct_signature(reference["final"])["materialIdsByLevelSetIndex"],
                    "productEngine": row.get("productActualEngine") == args.engine,
                    "directEngine": row.get("directActualEngine") == args.engine,
                    "versions": row.get("productVersions") == VERSIONS == row.get("directVersions"),
                }
                if kind == "fin":
                    comparisons["finConstructionFallback"] = (
                        product_geometry.get("construction") == direct_geometry.get("effective")
                        and product_geometry.get("requestedConstruction") == direct_geometry.get("requested")
                        and product_geometry.get("fallbackModels") == direct_geometry.get("fallbackModels")
                        and product_geometry.get("automaticFallback") ==
                            (direct_geometry.get("effective") != direct_geometry.get("requested"))
                        and product_geometry.get("extrusionRoute") == direct_geometry.get("extrusionRoute")
                        and product_geometry.get("extrusionFallbackModels") == direct_geometry.get("extrusionFallbackModels")
                        and product_geometry.get("automaticExtrusionFallback") ==
                            (direct_geometry.get("extrusionRoute") == "direct-stencil")
                    )
                    row["productGeometryReadback"] = product_geometry
                    row["directFinConstruction"] = direct_geometry
                row["comparisons"] = comparisons
                row["coordinateTolerance"] = 1e-12
                row["coordinateDeltaMaxAbs"] = {"initial": initial_point_delta, "final": final_point_delta}
                row["status"] = "verified" if all(comparisons.values()) else "mismatch"
                row["counts"] = {"initialPoints": len(initial["points"]), "finalPoints": len(final["points"]),
                                  "initialCells": len(product_signature(initial, dimension)["cells"]),
                                  "finalCells": len(product_signature(final, dimension)["cells"]),
                                  "initialMaterialMap": initial["materialIdsByLevelSetIndex"],
                                  "finalMaterialMap": final["materialIdsByLevelSetIndex"]}
            elif product.returncode != 0:
                row["status"] = "product-failed"
            elif direct.returncode != 0:
                row["status"] = "direct-failed"
            (evidence / "acceptance.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({key: row.get(key) for key in ("name", "status", "comparisons", "productFailure", "directFailure") if key in row}, ensure_ascii=False), flush=True)
            rows.append(row)
    summary = {"status": "passed" if all(row["status"] == "verified" for row in rows) else "incomplete",
               "passed": all(row["status"] == "verified" for row in rows), "versions": VERSIONS,
               "scope": "schema-7 specialized GPU_DISK models across plane, trench, hole and Fin presets; compared with direct ViennaPS",
               "rows": rows}
    summary_name = "summary.json" if not (args.geometry or args.model or args.dimension or args.fin_mode or args.fin_construction != "native" or args.fin_auto_fallback or args.mask_height is not None or args.engine != "GPU_DISK" or args.rate_scale != 1.0 or args.duration_scale != 1.0 or args.boundary != "reflective") else "targeted-summary.json"
    (EVIDENCE / summary_name).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
