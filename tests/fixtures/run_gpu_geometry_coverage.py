"""Bounded product-worker/direct GPU_DISK matrix for schema-7 preset geometries."""
from __future__ import annotations

import hashlib
import importlib.metadata
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage8_project_factory import make_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets"
RUN_ROOT = ROOT / "var/gpu-geometry-coverage"
DIRECT = ROOT / "tests/fixtures/gpu_geometry_direct_reference.py"
PYTHON = ROOT / ".venv/bin/python"


def geometry_cases() -> list[tuple[str, int, bool]]:
    return [
        ("plane", 2, False), ("plane", 3, False),
        ("trench", 2, False), ("trench", 3, False),
        ("hole", 3, False),
        ("fin", 2, False), ("fin", 3, False), ("fin", 3, True),
    ]


def geometry_input(kind: str, dimension: int, extruded: bool) -> dict[str, Any]:
    if kind == "plane":
        return {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    if kind == "trench":
        return {"kind": "trench", "width": 2.0, "depth": 2.2, "taperAngle": 0.0,
                "maskHeight": 0.0, "maskTaperAngle": 0.0,
                "substrateMaterial": "Si", "maskMaterial": "Mask"}
    if kind == "hole":
        if dimension != 3:
            raise ValueError("hole preset is 3D only")
        return {"kind": "hole", "radius": 1.0, "depth": 2.0, "taperAngle": 0.0,
                "maskHeight": 0.0, "maskTaperAngle": 0.0,
                "substrateMaterial": "Si", "maskMaterial": "Mask"}
    if kind == "fin":
        geometry: dict[str, Any] = {
            "kind": "fin", "width": 0.8, "height": 1.2, "taperAngle": 0.0,
            "maskHeight": 0.0, "maskTaperAngle": 0.0,
            "substrateMaterial": "Si", "maskMaterial": "Mask",
        }
        if dimension == 3 and extruded:
            geometry["extrusion"] = {"extent": [-1.0, 1.0], "axis": 1}
        return geometry
    raise ValueError(f"unknown preset {kind!r}")


def make_project(kind: str, dimension: int, extruded: bool, model: str, operation: str) -> dict[str, Any]:
    project = make_project_template(dimension)
    project["name"] = f"GPU preset {kind} {dimension}D {model} {operation} {'extruded' if extruded else 'direct'}"
    project["geometry"] = geometry_input(kind, dimension, extruded)
    if dimension == 2:
        project["domain"] = {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"}
    else:
        project["domain"] = {"gridDelta": 0.2, "xExtent": 4.0, "yExtent": 4.0, "boundaryXY": "reflective"}
    source = project["steps"][0]
    step: dict[str, Any] = {
        "id": "gpu_geometry_process",
        "name": f"{kind} {model} {operation}",
        "enabled": True,
        "operation": operation,
        "duration": 0.2,
    }
    if operation == "deposit":
        step["depositMaterial"] = "SiO2"
    if model == "singleParticle":
        step.update({"model": model, "rateMagnitude": 0.2, "stickingProbability": 0.5,
                     "sourceExponent": 1.0, "rateMode": "uniform"})
    else:
        step.update({
            "model": "multiParticle",
            "species": [
                {"kind": "ion", "label": "argonIon", "sourcePower": 1.0, "thetaRMin": 0.0,
                 "thetaRMax": 50.0, "minAngle": 60.0, "B_sp": -1.0, "meanEnergy": 120.0,
                 "sigmaEnergy": 15.0, "thresholdEnergy": 10.0, "inflectAngle": 25.0, "n": 1},
                {"kind": "neutral", "label": "fluorineNeutral", "defaultStickingProbability": 0.4,
                 "stickingProbabilityByMaterial": {}},
            ],
            "rateCoefficients": {"defaultBySpecies": {"argonIon": 0.025, "fluorineNeutral": 0.025},
                                 "byMaterial": {}},
        })
    project["steps"] = [step]
    project["solver"]["enginePreference"] = "gpu"
    project["solver"]["raysPerPoint"] = 64
    project["solver"]["random"] = {"mode": "fixed", "seed": 314159}
    project["solver"]["rayTracing"].update({"maxBoundaryHits": 80, "maxReflections": 3})
    if dimension == 3:
        project.pop("view", None)
    else:
        project.pop("view3D", None)
    return project


def safe_tag(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "material"


def configure_species_profile(step: dict[str, Any], profile: str | None) -> None:
    """Select native particle layouts used to debug the installed GPU binding."""
    if profile is None:
        return
    ion = {"kind": "ion", "label": "argonIon", "sourcePower": 1.0,
           "thetaRMin": 0.0, "thetaRMax": 50.0, "minAngle": 60.0,
           "B_sp": -1.0, "meanEnergy": 120.0, "sigmaEnergy": 15.0,
           "thresholdEnergy": 10.0, "inflectAngle": 25.0, "n": 1}
    neutral = {"kind": "neutral", "label": "fluorineNeutral",
               "defaultStickingProbability": 0.4, "stickingProbabilityByMaterial": {}}
    neutral_rows = [{**neutral, "label": f"neutral{index}",
                     "defaultStickingProbability": round(0.2 + 0.1 * index, 2)}
                    for index in range(7)]
    cases: dict[str, list[dict[str, Any]]] = {
        "one-ion": [ion],
        "one-ion-sourcepower-1_5": [{**ion, "sourcePower": 1.5}],
        "one-ion-one-neutral": [ion, neutral],
        "one-ion-two-neutrals": [ion, neutral, {**neutral, "label": "chlorineNeutral",
                                                    "defaultStickingProbability": 0.55}],
        "neutral-ion-neutral": [neutral, ion, {**neutral, "label": "chlorineNeutral",
                                                 "defaultStickingProbability": 0.55}],
        "two-neutrals": [neutral, {**neutral, "label": "chlorineNeutral",
                                    "defaultStickingProbability": 0.55}],
        "eight-neutrals": [{**neutral, "label": f"neutral{index}",
                            "defaultStickingProbability": round(0.2 + 0.1 * index, 2)}
                           for index in range(8)],
        "one-ion-seven-neutrals-first": [ion, *neutral_rows],
        "one-ion-seven-neutrals-last": [*neutral_rows, {**ion, "label": "argonIonLast"}],
        "two-ion-same-distribution": [ion, {**ion, "label": "argonIonB", "sourcePower": 0.5}, neutral],
        "two-ion-different-energy": [ion, {**ion, "label": "xenonIon", "meanEnergy": 180.0}, neutral],
        "ion-neutral-ion": [ion, neutral, {**ion, "label": "argonIonB", "sourcePower": 0.5}],
        "eight-ions": [
            {**ion, "label": f"ion{index}", "meanEnergy": 40.0 + 10.0 * index}
            for index in range(8)
        ],
    }
    species = cases[profile]
    step["species"] = species
    step["rateCoefficients"] = {"defaultBySpecies": {item["label"]: 0.025 for item in species},
                                "byMaterial": {}}


def make_project_template(dimension: int) -> dict[str, Any]:
    return make_project_factory("sf6o2", dimension)


def make_project_factory(model_id: str, dimension: int) -> dict[str, Any]:
    # The imported factory creates the installed version's complete schema-7
    # solver defaults; the specialized process is replaced below.
    return stage8_make_project(model_id, dimension)


from stage8_project_factory import make_project as stage8_make_project


def bounded(name: str, kind: str, command: list[str], evidence: Path) -> subprocess.CompletedProcess[str]:
    log_path = evidence / f"{kind}.log"
    log_path.unlink(missing_ok=True)
    child_env = os.environ.copy()
    runtime_dir = child_env.get("VIENNAPS_PATCHED_RUNTIME_DIR")
    if runtime_dir:
        child_env["PYTHONPATH"] = os.pathsep.join(
            [runtime_dir, str(ROOT), child_env.get("PYTHONPATH", "")]
        )
    return subprocess.run([
        str(PYTHON), str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
        "--record", str(evidence / f"{kind}.guard.json"),
        "--log-path", str(log_path),
        "--timeout-seconds", "120", "--max-rss-mib", "1800",
        "--max-log-bytes", "2000000", "--terminate-grace-seconds", "2", "--",
        *command,
    ], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=132, check=False)


def mesh_signature(value: dict[str, Any], dimension: int) -> dict[str, Any]:
    return {
        "points": value["points"],
        "cells": value["lines"] if dimension == 2 else value["triangles"],
        "materialIds": value["cellData"]["materialIds"],
        "materialIdsByLevelSetIndex": value["materialIdsByLevelSetIndex"],
    }


def direct_signature(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def make_cases(args: argparse.Namespace):
    for kind, dimension, extruded in geometry_cases():
        if args.geometry and kind not in args.geometry:
            continue
        if args.dimension and dimension not in args.dimension:
            continue
        if kind == "fin" and args.fin_mode and ("extruded" if extruded else "direct") != args.fin_mode:
            continue
        for model in ("singleParticle", "multiParticle"):
            if args.model and model not in args.model:
                continue
            for operation in ("etch", "deposit"):
                if args.operation and operation not in args.operation:
                    continue
                yield kind, dimension, extruded, model, operation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", action="append", choices=("plane", "trench", "hole", "fin"))
    parser.add_argument("--model", action="append", choices=("singleParticle", "multiParticle"))
    parser.add_argument("--operation", action="append", choices=("etch", "deposit"))
    parser.add_argument("--dimension", action="append", type=int, choices=(2, 3))
    parser.add_argument("--fin-mode", choices=("direct", "extruded"))
    parser.add_argument("--fin-construction", choices=("native", "boolean"), default="native")
    parser.add_argument("--mask-height", type=float)
    parser.add_argument("--boundary", choices=("reflective", "periodic"), default="reflective")
    parser.add_argument("--engine", choices=("GPU_DISK", "CPU_DISK"), default="GPU_DISK")
    parser.add_argument("--time-step-ratio", type=float,
                        help="override the schema-7 advection CFL/time-step ratio")
    parser.add_argument("--rays-per-point", type=int, help="override the native ray count")
    parser.add_argument("--substrate-material", default="Si")
    parser.add_argument("--deposit-material", default="SiO2")
    parser.add_argument("--mask-material", default="Mask")
    parser.add_argument("--custom-material-id", type=int, choices=(177, 240),
                        help="exercise a registered custom material at the first or last installed custom native ID")
    parser.add_argument("--custom-material-role", choices=("substrate", "deposit", "mask"), default="substrate")
    parser.add_argument("--all-custom-material-tables", action="store_true",
                        help="configure native rate/sticking tables for every custom identity through ID 240")
    parser.add_argument("--single-material-rates", action="store_true",
                        help="use distinct native setMaterialRate values for every initial geometry material")
    parser.add_argument("--multi-material-rates", action="store_true",
                        help="use distinct MultiParticle rate coefficients for every initial geometry material")
    parser.add_argument("--multi-material-sticking", action="store_true",
                        help="set a material-specific neutral sticking value for every initial geometry material")
    parser.add_argument("--species-profile", choices=("one-ion", "one-ion-sourcepower-1_5", "one-ion-one-neutral",
                        "one-ion-two-neutrals", "neutral-ion-neutral", "two-neutrals", "eight-neutrals",
                        "one-ion-seven-neutrals-first", "one-ion-seven-neutrals-last", "two-ion-same-distribution",
                        "two-ion-different-energy", "ion-neutral-ion", "eight-ions"),
                        help="probe a bounded MultiParticle GPU particle-type combination")
    args = parser.parse_args()
    selected_models = set(args.model or ("singleParticle", "multiParticle"))
    selected_operations = set(args.operation or ("etch", "deposit"))
    if args.single_material_rates and ("singleParticle" not in selected_models or "etch" not in selected_operations):
        raise SystemExit("--single-material-rates requires at least one SingleParticle etch case")
    if (args.multi_material_rates or args.multi_material_sticking) and "multiParticle" not in selected_models:
        raise SystemExit("MultiParticle material tables require at least one MultiParticle case")
    if args.species_profile and (selected_models != {"multiParticle"} or selected_operations != {"etch"}):
        raise SystemExit("--species-profile requires a MultiParticle etch case")
    if args.time_step_ratio is not None and not (0.0 < args.time_step_ratio <= 1.0):
        raise SystemExit("--time-step-ratio must be in (0, 1]")
    if args.rays_per_point is not None and args.rays_per_point <= 0:
        raise SystemExit("--rays-per-point must be positive")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    rows = []
    for kind, dimension, extruded, model, operation in make_cases(args):
        custom_definitions: list[dict[str, Any]] = []
        substrate_material = args.substrate_material
        deposit_material = args.deposit_material
        mask_material = args.mask_material
        if args.custom_material_id is not None:
            count = args.custom_material_id - 177 + 1
            custom_definitions = [{
                "name": f"GpuCustom{index:02d}", "id": 177 + index, "customId": index,
                "color": "#7755aa", "conductive": False,
                "description": "bounded generic GPU material identity acceptance",
            } for index in range(count)]
            custom_name = custom_definitions[-1]["name"]
            if args.custom_material_role == "substrate":
                substrate_material = custom_name
            elif args.custom_material_role == "deposit":
                deposit_material = custom_name
            else:
                mask_material = custom_name
        suffix = "extruded" if extruded else "direct"
        construction_tag = "-boolean" if kind == "fin" and args.fin_construction == "boolean" else ""
        mask_tag = f"-mask-{args.mask_height:g}" if kind in {"trench", "hole", "fin"} and args.mask_height is not None else ""
        substrate_tag = "" if args.substrate_material == "Si" else f"-bulk-{safe_tag(args.substrate_material)}"
        deposit_tag = (f"-depositmat-{safe_tag(deposit_material)}"
                       if operation == "deposit" and deposit_material != "SiO2" else "")
        mask_material_tag = (f"-maskmat-{safe_tag(mask_material)}"
                             if mask_material != "Mask" and kind in {"trench", "hole", "fin"}
                             and args.mask_height is not None else "")
        rate_tag = ("-single-material-rates" if args.single_material_rates else "")
        rate_tag += "-multi-material-rates" if args.multi_material_rates else ""
        rate_tag += "-multi-material-sticking" if args.multi_material_sticking else ""
        if args.time_step_ratio is not None:
            rate_tag += f"-cfl{args.time_step_ratio:g}"
        if args.rays_per_point is not None:
            rate_tag += f"-rays{args.rays_per_point}"
        boundary_tag = "-periodic" if args.boundary == "periodic" else ""
        custom_tag = f"-custom-id{args.custom_material_id}" if args.custom_material_id is not None else ""
        if args.custom_material_id is not None and args.custom_material_role != "substrate":
            custom_tag += f"-{args.custom_material_role}"
        if args.custom_material_id is not None:
            substrate_tag = ""
        species_tag = f"-species-{args.species_profile}" if args.species_profile else ""
        name = f"{model}-{dimension}d-{kind}-{suffix}{construction_tag}{mask_tag}{mask_material_tag}{substrate_tag}{deposit_tag}{custom_tag}{rate_tag}{species_tag}{boundary_tag}-{operation}-{args.engine.lower()}"
        evidence = EVIDENCE / name
        prior_acceptance = evidence / "acceptance.json"
        if prior_acceptance.is_file():
            try:
                prior_status = json.loads(prior_acceptance.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError):
                prior_status = "unreadable"
            if prior_status != "verified":
                archive = EVIDENCE / "known-failures" / f"{name}-{int(prior_acceptance.stat().st_mtime)}"
                archive.parent.mkdir(parents=True, exist_ok=True)
                if not archive.exists():
                    shutil.copytree(evidence, archive)
        evidence.mkdir(parents=True, exist_ok=True)
        project = make_project(kind, dimension, extruded, model, operation)
        configure_species_profile(project["steps"][0], args.species_profile)
        project["solver"]["enginePreference"] = "gpu" if args.engine.startswith("GPU_") else "cpu"
        if args.engine.startswith("GPU_"):
            visible_uuid = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",", 1)[0].strip()
            if visible_uuid.startswith("GPU-"):
                project["solver"]["gpuDeviceId"] = visible_uuid
        project["geometry"]["substrateMaterial"] = substrate_material
        if "maskMaterial" in project["geometry"]:
            project["geometry"]["maskMaterial"] = mask_material
        if args.custom_material_id is not None and args.custom_material_role == "mask" and args.mask_height is None:
            project["geometry"]["maskHeight"] = 0.6
        if dimension == 2:
            project["domain"]["xBoundary"] = args.boundary
        else:
            project["domain"]["boundaryXY"] = args.boundary
        if args.time_step_ratio is not None:
            project["solver"]["timeStepRatio"] = float(args.time_step_ratio)
        if args.rays_per_point is not None:
            project["solver"]["raysPerPoint"] = int(args.rays_per_point)
        if kind == "fin":
            project["geometry"]["construction"] = args.fin_construction
        if kind in {"trench", "hole", "fin"} and args.mask_height is not None:
            project["geometry"]["maskHeight"] = args.mask_height
        if operation == "deposit":
            project["steps"][0]["depositMaterial"] = deposit_material
        if custom_definitions:
            project["materialDefinitions"] = custom_definitions
            if dimension == 3:
                project["view3D"]["visibleMaterialIds"].append(args.custom_material_id)
                project["view3D"]["opacityByMaterial"][str(args.custom_material_id)] = 1.0
        active_materials = {substrate_material}
        if kind in {"trench", "hole", "fin"} and float(project["geometry"].get("maskHeight", 0)) > 0:
            active_materials.add(mask_material)
        if operation == "deposit":
            active_materials.add(deposit_material)
        configured_materials = set(active_materials)
        if args.all_custom_material_tables:
            if args.custom_material_id != 240:
                raise SystemExit("--all-custom-material-tables requires --custom-material-id 240")
            configured_materials.update(row["name"] for row in custom_definitions)
        if args.single_material_rates:
            if model == "singleParticle" and operation == "etch":
                project["steps"][0]["rateMode"] = "material"
                project["steps"][0]["materialRates"] = {
                    name: round(0.04 + 0.01 * index, 6)
                    for index, name in enumerate(sorted(configured_materials))
                }
        if args.multi_material_rates:
            if model == "multiParticle":
                relation = project["steps"][0]["rateCoefficients"]
                relation["byMaterial"] = {
                    name: {"argonIon": round(0.012 + 0.001 * index, 6),
                           "fluorineNeutral": round(0.017 + 0.002 * index, 6)}
                    for index, name in enumerate(sorted(configured_materials))
                }
        if args.multi_material_sticking:
            if model == "multiParticle":
                neutral = next(species for species in project["steps"][0]["species"]
                               if species["kind"] == "neutral")
                neutral["stickingProbabilityByMaterial"] = {
                    name: round(0.2 + 0.75 * index / max(len(configured_materials) - 1, 1), 6)
                    for index, name in enumerate(sorted(configured_materials))
                }
        project_path = evidence / "project.json"
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        digest = hashlib.sha256(name.encode()).hexdigest()[:24]
        run_dir = RUN_ROOT / digest
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, mode=0o700)
        config_path = run_dir / "input.json"
        config = {
            "kind": "simulate", "runId": digest, "project": project,
            "runSeed": project["solver"]["random"]["seed"],
            "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                 "viennals": importlib.metadata.version("viennals")},
            "stepEngines": {project["steps"][0]["id"]: args.engine}, "customMaterials": custom_definitions,
        }
        gpu_multi_particle = args.engine == "GPU_DISK" and model == "multiParticle"
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
        selected_uuid = project["solver"].get("gpuDeviceId")
        if args.engine.startswith("GPU_") and selected_uuid:
            config["selectedGpuDevice"] = {"uuid": selected_uuid}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        product = bounded(name, "product",
                          [str(PYTHON), str(ROOT / "worker/simulate.py"), str(config_path), str(run_dir)], evidence)
        direct_path = evidence / "direct.json"
        direct = bounded(name, "direct", [
            str(PYTHON), str(DIRECT), str(project_path), str(direct_path), str(run_dir / "direct-scratch"), args.engine,
        ], evidence)
        row: dict[str, Any] = {
            "name": name, "status": "failed", "dimension": dimension, "geometry": kind,
            "finExtrusionMode": ("extruded" if extruded else "direct") if kind == "fin" else None,
            "finConstruction": args.fin_construction if kind == "fin" else None,
            "model": model, "operation": operation, "engine": args.engine,
            "productReturnCode": product.returncode, "directReturnCode": direct.returncode,
            "project": str(project_path.relative_to(ROOT)),
            "productGuard": str((evidence / "product.guard.json").relative_to(ROOT)),
            "directGuard": str((evidence / "direct.guard.json").relative_to(ROOT)),
            "productLog": str((evidence / "product.log").relative_to(ROOT)),
            "directLog": str((evidence / "direct.log").relative_to(ROOT)),
            "directReference": str(direct_path.relative_to(ROOT)),
        }
        if product.returncode != 0:
            row["productFailure"] = (product.stderr or product.stdout)[-3000:]
        if direct.returncode != 0:
            row["directFailure"] = (direct.stderr or direct.stdout)[-3000:]
        result_path = run_dir / "worker-result.json"
        if product.returncode == 0 and result_path.is_file():
            worker = json.loads(result_path.read_text(encoding="utf-8"))
            if gpu_multi_particle and worker.get("nativeEngineFingerprint") != runtime_fingerprint:
                raise RuntimeError("worker did not load the configured patched ViennaPS runtime")
            product_initial_row = worker["snapshots"][0]
            product_final_row = worker["snapshots"][-1]
            initial_path = run_dir / product_initial_row["snapshot"]
            final_path = run_dir / product_final_row["snapshot"]
            initial = json.loads(initial_path.read_text(encoding="utf-8"))
            final = json.loads(final_path.read_text(encoding="utf-8"))
            row["productActualEngine"] = worker["stepRecords"][0]["actualEngine"]
            row["productVersions"] = worker["versions"]
            row["productStepId"] = worker["stepRecords"][0]["stepId"]
            row["productActualParameters"] = worker["stepRecords"][0].get("actualParameters", {})
            row["productCallbacks"] = worker["stepRecords"][0]["actualParameters"].get("rateCallbackSummary")
            shutil.copy2(result_path, evidence / "worker-result.json")
            shutil.copy2(config_path, evidence / "worker-input.json")
            shutil.copy2(initial_path, evidence / "initial.json")
            shutil.copy2(final_path, evidence / "final.json")
            shutil.copy2(run_dir / product_initial_row["vtk"], evidence / "initial.vtk")
            shutil.copy2(run_dir / product_final_row["vtk"], evidence / "final.vtk")
        else:
            initial = final = None
        if direct.returncode == 0 and direct_path.is_file():
            reference = json.loads(direct_path.read_text(encoding="utf-8"))
            if gpu_multi_particle and not Path(reference["viennapsModulePath"]).resolve().is_relative_to(runtime_root / "viennaps"):
                raise RuntimeError("direct reference did not load the configured patched ViennaPS runtime")
            row["directActualEngine"] = reference["actualEngine"]
            row["directVersions"] = reference["versions"]
        else:
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
                "productEngine": row.get("productActualEngine") == args.engine,
                "directEngine": row.get("directActualEngine") == args.engine,
                "versions": row.get("productVersions") == {"viennaps": "4.6.2", "viennals": "5.8.5"} == row.get("directVersions"),
            }
            direct_parameters = reference.get("actualParameters", {})
            product_parameters = row.get("productActualParameters", {})
            if model == "singleParticle":
                comparisons["singleParticleMaterialRates"] = (
                    product_parameters.get("rateMode", "uniform") == direct_parameters.get("rateMode", "uniform")
                    and product_parameters.get("actualDefaultRate") == direct_parameters.get("actualDefaultRate")
                    and product_parameters.get("actualMaterialRatesByName", {}) == direct_parameters.get("actualMaterialRatesByName", {})
                    and product_parameters.get("stickingProbability") == direct_parameters.get("stickingProbability")
                    and product_parameters.get("sourceExponent") == direct_parameters.get("sourceExponent")
                )
            else:
                comparisons["multiParticleSpeciesConfiguration"] = (
                    product_parameters.get("multiParticle", {}).get("speciesOrder") == direct_parameters.get("speciesOrder")
                    and product_parameters.get("multiParticle", {}).get("species") == direct_parameters.get("species")
                    and product_parameters.get("multiParticle", {}).get("rateFunction") == direct_parameters.get("rateFunction")
                )
            if kind == "fin":
                product_geometry = worker.get("snapshots", [{}])[0].get("geometryReadback", {})
                direct_geometry = reference.get("finConstruction", {})
                comparisons["finConstructionRoute"] = (
                    product_geometry.get("construction") == direct_geometry.get("effective")
                    and product_geometry.get("requestedConstruction") == direct_geometry.get("requested")
                    and product_geometry.get("fallbackModels") == direct_geometry.get("fallbackModels")
                    and product_geometry.get("constructionFallbackModels") == direct_geometry.get("constructionFallbackModels")
                    and product_geometry.get("constructionFallbackReason") == direct_geometry.get("constructionFallbackReason")
                    and product_geometry.get("automaticFallback") ==
                        (direct_geometry.get("effective") != direct_geometry.get("requested"))
                    and product_geometry.get("extrusionRoute") == direct_geometry.get("extrusionRoute")
                    and product_geometry.get("extrusionFallbackModels") == direct_geometry.get("extrusionFallbackModels")
                    and product_geometry.get("automaticExtrusionFallback") ==
                        (direct_geometry.get("extrusionRoute") == "direct-stencil")
                )
                row["productGeometryReadback"] = product_geometry
                row["directFinConstruction"] = direct_geometry
            if model == "multiParticle":
                product_callbacks = row.get("productCallbacks") or {}
                product_materials = product_callbacks.get("materials", {})
                direct_callbacks = reference.get("callback") or {}
                comparisons["rateCallbackCount"] = product_callbacks.get("invocationCount") == direct_callbacks.get("count")
                comparisons["rateCallbackMaterials"] = product_materials == direct_callbacks.get("materials")
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
            row["counts"] = {
                "initialPoints": len(initial["points"]), "finalPoints": len(final["points"]),
                "initialCells": len(mesh_signature(initial, dimension)["cells"]),
                "finalCells": len(mesh_signature(final, dimension)["cells"]),
                "initialMaterialMap": initial["materialIdsByLevelSetIndex"],
                "finalMaterialMap": final["materialIdsByLevelSetIndex"],
            }
        elif product.returncode != 0:
            row["status"] = "product-failed"
        elif direct.returncode != 0:
            row["status"] = "direct-failed"
        (evidence / "acceptance.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8")
        if row["status"] != "verified":
            archive = EVIDENCE / "known-failures" / f"{name}-{time.time_ns()}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            row["failureArchive"] = str(archive.relative_to(ROOT))
            (evidence / "acceptance.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                                                        encoding="utf-8")
            shutil.copytree(evidence, archive)
        print(json.dumps({key: row.get(key) for key in ("name", "status", "comparisons",
                                                        "productFailure", "directFailure") if key in row},
                         ensure_ascii=False), flush=True)
        rows.append(row)
    summary = {
        "status": "passed" if all(row["status"] == "verified" for row in rows) else "incomplete",
        "passed": all(row["status"] == "verified" for row in rows),
        "versions": {"viennaps": importlib.metadata.version("viennaps"),
                     "viennals": importlib.metadata.version("viennals")},
        "scope": "schema-7 CPU/GPU_DISK exact-reference coverage for plane, trench, hole and Fin preset geometry",
        "rows": rows,
    }
    summary_name = "targeted-summary.json" if (args.geometry or args.dimension or args.fin_mode
        or args.fin_construction != "native" or args.mask_height is not None
        or args.boundary != "reflective" or args.model or args.operation or args.engine != "GPU_DISK") else "summary.json"
    (EVIDENCE / summary_name).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
