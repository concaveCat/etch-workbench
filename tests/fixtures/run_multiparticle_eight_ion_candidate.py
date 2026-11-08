"""Compare eight-ion direct ViennaPS runs on CPU and the patched RTX runtime.

This orchestrator does not import ViennaPS. Every native call is launched
under scripts/bounded_child.py and writes an independent reference snapshot.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product"
DEVICE_UUID = "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    runtime = args.runtime_dir.resolve(strict=True)
    if not (runtime / "viennaps" / "_core.cpython-312-x86_64-linux-gnu.so").is_file():
        raise SystemExit("runtime-dir does not contain the expected ViennaPS 4.6.2 extension")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report_path = EVIDENCE / "eight-ion-direct-comparison.json"
    report: dict[str, object] = {
        "status": "running",
        "runtimeDir": str(runtime),
        "deviceUuid": DEVICE_UUID,
        "cases": [],
    }

    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(runtime), str(ROOT), env.get("PYTHONPATH", "")])
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = DEVICE_UUID
    case_data: dict[tuple[int, str], dict] = {}
    try:
        for dimension in (2, 3):
            project = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets" / (
                f"multiParticle-{dimension}d-plane-direct-species-eight-ions-etch-gpu_disk/project.json"
            )
            if not project.is_file():
                raise FileNotFoundError(project)
            for engine in ("CPU_DISK", "GPU_DISK"):
                stem = f"eight-ion-{dimension}d-{engine.lower()}"
                output = EVIDENCE / f"{stem}.json"
                guard = EVIDENCE / f"{stem}.guard.json"
                log = EVIDENCE / f"{stem}.log"
                scratch = ROOT / "var" / "eight-ion-candidate-scratch" / stem
                command = [
                    sys.executable,
                    str(ROOT / "scripts/bounded_child.py"),
                    "--cwd", str(ROOT),
                    "--record", str(guard),
                    "--log-path", str(log),
                    "--parent-pid", str(os.getpid()),
                    "--timeout-seconds", "120",
                    "--max-rss-mib", "2200",
                    "--max-log-bytes", "2000000",
                    "--",
                    sys.executable,
                    str(ROOT / "tests/fixtures/gpu_geometry_direct_reference.py"),
                    str(project), str(output), str(scratch), engine,
                ]
                completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                           text=True, timeout=145, check=False)
                row: dict[str, object] = {
                    "dimension": dimension,
                    "engine": engine,
                    "returnCode": completed.returncode,
                    "output": str(output.relative_to(ROOT)),
                    "guard": str(guard.relative_to(ROOT)),
                    "log": str(log.relative_to(ROOT)),
                }
                report["cases"].append(row)  # type: ignore[union-attr]
                save()
                if completed.returncode != 0:
                    raise RuntimeError(f"{dimension}D {engine} failed; inspect {log}")
                data = json.loads(output.read_text(encoding="utf-8"))
                labels = data["actualParameters"]["speciesOrder"]
                expected = [f"ion{i}" for i in range(8)]
                samples = data["callback"]["samples"]
                if data["status"] != "succeeded" or data["actualEngine"] != engine:
                    raise RuntimeError(f"{dimension}D {engine} returned unexpected status/engine")
                if not Path(data["viennapsModulePath"]).resolve().is_relative_to(runtime):
                    raise RuntimeError(f"{dimension}D {engine} did not load the requested candidate runtime")
                if labels != expected or not samples:
                    raise RuntimeError(f"{dimension}D {engine} did not preserve all eight species labels")
                if any(len(sample["fluxes"]) != 8 for sample in samples):
                    raise RuntimeError(f"{dimension}D {engine} callback flux vector is not eight species wide")
                if any(not math.isfinite(value) for sample in samples for value in sample["fluxes"]):
                    raise RuntimeError(f"{dimension}D {engine} returned a non-finite callback flux")
                row["speciesOrder"] = labels
                row["callbackCount"] = data["callback"]["count"]
                row["callbackSamplesChecked"] = len(samples)
                row["meanFluxBySpecies"] = {
                    label: data["callback"]["meanFluxBySpecies"][index]
                    for index, label in enumerate(labels)
                }
                row["sampleFluxBySpecies"] = {
                    label: [sample["fluxes"][index] for sample in samples]
                    for index, label in enumerate(labels)
                }
                row["finalPoints"] = len(data["final"]["points"])
                row["finalCells"] = len(data["final"]["cells"])
                case_data[(dimension, engine)] = data
                save()

        comparisons = []
        for dimension in (2, 3):
            cpu_data = case_data[(dimension, "CPU_DISK")]
            gpu_data = case_data[(dimension, "GPU_DISK")]
            cpu = cpu_data["final"]
            gpu = gpu_data["final"]
            cpu_points, gpu_points = cpu["points"], gpu["points"]
            same_points = len(cpu_points) == len(gpu_points)
            max_point_delta = None
            if same_points:
                max_point_delta = max(
                    (abs(float(a) - float(b)) for row_a, row_b in zip(cpu_points, gpu_points)
                     for a, b in zip(row_a, row_b)), default=0.0
                )
            comparison = {
                "dimension": dimension,
                "samePointCount": same_points,
                "sameCellConnectivity": cpu["cells"] == gpu["cells"],
                "sameMaterialIds": cpu["materialIds"] == gpu["materialIds"],
                "sameMaterialMap": cpu["materialIdsByLevelSetIndex"] == gpu["materialIdsByLevelSetIndex"],
                "maxCoordinateDelta": max_point_delta,
                "meanFluxRelativeDifferenceBySpecies": {
                    label: abs(cpu_data["callback"]["meanFluxBySpecies"][index]
                               - gpu_data["callback"]["meanFluxBySpecies"][index])
                    / max(abs(cpu_data["callback"]["meanFluxBySpecies"][index]), 1e-30)
                    for index, label in enumerate(cpu_data["actualParameters"]["speciesOrder"])
                },
            }
            comparisons.append(comparison)
        report["comparisons"] = comparisons
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    save()
    print(json.dumps({"status": report["status"], "report": str(report_path),
                      "error": report.get("error")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
