"""Bounded product/direct GPU device-selection readback acceptance."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from backend.gpu_devices import cuda_devices, resolve_gpu_device
from backend.runtime_limits import available_cpu_threads
from run_gpu_singleparticle_coverage import make_generic_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection"
RUNS = ROOT / "var/gpu-device-selection-probe"


def bounded(name: str, command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    record = EVIDENCE / f"{name}.guard.json"
    log = EVIDENCE / f"{name}.log"
    record.unlink(missing_ok=True)
    log.unlink(missing_ok=True)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
         "--record", str(record),
         "--log-path", str(log), "--timeout-seconds", "180",
         "--max-rss-mib", "1800", "--max-log-bytes", "2000000", "--",
         *command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=190, check=False)


def signature(snapshot: dict, dimension: int) -> dict:
    cells = snapshot.get("lines" if dimension == 2 else "triangles", snapshot.get("cells"))
    material_ids = snapshot.get("cellData", {}).get("materialIds", snapshot.get("materialIds"))
    return {"points": snapshot["points"], "cells": cells,
            "materialIds": material_ids,
            "materialMap": snapshot["materialIdsByLevelSetIndex"]}


def normalize_pci(value: str) -> str:
    parts = value.lower().split(":")
    return f"{parts[0][-4:]}:{parts[1]}:{parts[2]}" if len(parts) == 3 else value.lower()


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    device = resolve_gpu_device(None, cuda_devices(refresh=True))
    project = make_generic_project(3, "etch", "none")
    project["name"] = "RTX 3060 selected-device 3D smoke"
    project["domain"].update(gridDelta=0.05, xExtent=4.0, yExtent=4.0)
    project["solver"].update(enginePreference="gpu", gpuDeviceId=device["uuid"],
                              raysPerPoint=128, cpuThreads=available_cpu_threads())
    step = project["steps"][0]
    name = "rtx3060-3d-product-direct"
    project_path = EVIDENCE / f"{name}.project.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_dir = RUNS / "product"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, mode=0o700)
    config = {"kind": "simulate", "runId": name, "project": project,
              "runSeed": project["solver"]["random"]["seed"],
              "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                   "viennals": importlib.metadata.version("viennals")},
              "stepEngines": {step["id"]: "GPU_DISK"}, "customMaterials": [],
              "selectedGpuDevice": device}
    input_path = run_dir / "input.json"
    input_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    product = bounded(name + ".product", [str(ROOT / ".venv/bin/python"),
                       str(ROOT / "worker/simulate.py"), str(input_path), str(run_dir)])
    product_result_path = run_dir / "worker-result.json"
    if product.returncode != 0 or not product_result_path.is_file():
        raise RuntimeError(f"GPU product worker failed: rc={product.returncode}; {product.stderr[-3000:]}")
    product_result = json.loads(product_result_path.read_text(encoding="utf-8"))
    direct_path = EVIDENCE / f"{name}.direct.json"
    direct_env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                  "CUDA_VISIBLE_DEVICES": device["uuid"]}
    direct = bounded(name + ".direct", [str(ROOT / ".venv/bin/python"),
                      str(ROOT / "tests/fixtures/gpu_singleparticle_direct_reference.py"),
                      str(project_path), str(direct_path)], env=direct_env)
    if direct.returncode != 0 or not direct_path.is_file():
        raise RuntimeError(f"direct ViennaPS reference failed: rc={direct.returncode}; {direct.stderr[-3000:]}")
    reference = json.loads(direct_path.read_text(encoding="utf-8"))
    initial_meta, final_meta = product_result["snapshots"][0], product_result["snapshots"][-1]
    product_initial = json.loads((run_dir / initial_meta["snapshot"]).read_text(encoding="utf-8"))
    product_final = json.loads((run_dir / final_meta["snapshot"]).read_text(encoding="utf-8"))
    step_record = product_result["stepRecords"][0]
    gpu_readback = step_record["actualParameters"]["cudaDeviceReadback"]
    process_visibility = gpu_readback["process"]
    process_list_consistent = (
        process_visibility["processListStatus"] in {"verified", "not-observed", "unavailable"}
        and process_visibility["processListed"] is (process_visibility["processListStatus"] == "verified")
        and (process_visibility["processListStatus"] != "verified" or process_visibility["gpuUuid"] == device["uuid"])
    )
    comparisons = {
        "initial": signature(product_initial, 3) == signature(reference["initial"], 3),
        "final": signature(product_final, 3) == signature(reference["final"], 3),
        "projectGpuUuid": project["solver"]["gpuDeviceId"] == device["uuid"],
        "cudaRuntimeUuid": product_result["cudaDeviceReadback"]["uuid"] == device["uuid"],
        "cudaRuntimePci": normalize_pci(product_result["cudaDeviceReadback"]["pciBusId"]) == normalize_pci(device["pciBusId"]),
        "cudaDeviceVerifiedAfterApply": gpu_readback["afterProcessApply"]["uuid"] == device["uuid"]
            and gpu_readback["afterProcessApply"]["synchronizeStatus"] == 0
            and gpu_readback["afterProcessApply"]["readbackStatus"] == "verified",
        "nvidiaSmiProcessVisibilityRecordConsistent": process_list_consistent,
        "productEngine": step_record["actualEngine"] == "GPU_DISK",
        "directEngine": reference["engine"] == "GPU_DISK",
    }
    report = {"status": "passed" if all(comparisons.values()) else "mismatch",
              "installedVersions": product_result["versions"], "device": device,
              "cudaRuntimeReadback": product_result["cudaDeviceReadback"],
              "processReadback": process_visibility,
              "processApplySeconds": step_record["actualParameters"]["processApplySeconds"],
              "cpuThreads": product_result["cpuThreads"], "comparisons": comparisons,
              "project": str(project_path.relative_to(ROOT)),
              "productWorker": str(product_result_path.relative_to(ROOT)),
              "productGuard": str((EVIDENCE / f"{name}.product.guard.json").relative_to(ROOT)),
              "directReference": str(direct_path.relative_to(ROOT)),
              "directGuard": str((EVIDENCE / f"{name}.direct.guard.json").relative_to(ROOT))}
    report_path = EVIDENCE / "selection-product-direct-acceptance.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
