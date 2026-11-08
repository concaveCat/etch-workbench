"""Bounded CPU/GPU sweep for exact MultiParticle operator-split profiles.

All ViennaPS calls run below scripts/bounded_child.py. This runner only adapts
saved direct-reference outputs into the shared comparator's input shape.
"""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
BASE_PROJECT = ROOT / (
    "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/"
    "geometry-scope/2d-trench-two-ion-cfl-025-ray64/project.json"
)
OUTPUT = ROOT / (
    "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/operator-split/"
    "geometry-scope/2d-trench-profile-sweep"
)
PROFILES = ((0.125, 64), (0.25, 256), (0.125, 256))


def run_bounded(output: Path, name: str, command: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
         "--record", str(output / f"{name}.guard.json"),
         "--log-path", str(output / f"{name}.log"),
         "--timeout-seconds", str(timeout), "--max-rss-mib", "3000",
         "--max-log-bytes", "1000000", "--", *command],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout + 15, check=False,
    )


def normalize_cpu(raw: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    step = next(row for row in project["steps"] if row.get("enabled"))
    return {
        "versions": raw["versions"],
        "initial": raw["initial"],
        "steps": {step["id"]: {"snapshot": raw["final"], "callback": raw["callback"],
                                "engine": raw["actualEngine"], "seed": raw["seed"]}},
    }


def normalize_gpu(raw: dict[str, Any]) -> dict[str, Any]:
    execution = raw["execution"]
    elapsed = sum(float(row["processApplySeconds"]) for row in execution["stages"])
    return {
        "status": raw["status"],
        "engine": raw["engine"],
        "contextDeviceIndex": int(raw["device"]["index"]),
        "cases": [{
            "sweeps": execution["sweeps"],
            "nativeStages": execution["nativeProcessCount"],
            "totalElapsedSeconds": elapsed,
            "callbacksBySpecies": execution["callbacksBySpecies"],
            "initial": raw["initial"],
            "final": raw["final"],
            "stages": execution["stages"],
        }],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=314159)
    parser.add_argument("--profile", action="append", default=[], metavar="CFL:RAYS",
                        help="solver profile to run; may be repeated (default: full sweep)")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    profiles = tuple((float(item.split(":", 1)[0]), int(item.split(":", 1)[1]))
                     for item in args.profile) if args.profile else PROFILES
    base = json.loads(BASE_PROJECT.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for cfl, rays in profiles:
        name = f"2d-trench-two-ion-cfl-{str(cfl).replace('.', '')}-ray{rays}-seed-{args.seed}"
        project = json.loads(json.dumps(base))
        project["name"] = f"2D trench two-ion MultiParticle profile sweep {cfl} / {rays}"
        project["solver"]["timeStepRatio"] = cfl
        project["solver"]["raysPerPoint"] = rays
        project["solver"]["random"]["seed"] = args.seed
        project_path = output / f"{name}.project.json"
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        cpu_raw_path = output / f"{name}.cpu-direct-raw.json"
        cpu_raw_scratch = output / f"{name}.cpu-scratch"
        cpu_raw_scratch.mkdir(parents=True, exist_ok=True)
        cpu_run = run_bounded(output, name + ".cpu", [str(PYTHON),
            str(ROOT / "tests/fixtures/gpu_geometry_direct_reference.py"),
            str(project_path), str(cpu_raw_path), str(cpu_raw_scratch), "CPU_DISK"])
        cpu = json.loads(cpu_raw_path.read_text(encoding="utf-8")) if cpu_raw_path.is_file() else None
        cpu_path = output / f"{name}.cpu-reference.json"
        if cpu is not None:
            cpu_path.write_text(json.dumps(normalize_cpu(cpu, project), ensure_ascii=False,
                                            allow_nan=False, indent=2) + "\n", encoding="utf-8")

        gpu_path = output / f"{name}.gpu-split.json"
        gpu_scratch = output / f"{name}.gpu-scratch"
        gpu_scratch.mkdir(parents=True, exist_ok=True)
        gpu_run = run_bounded(output, name + ".gpu", [str(PYTHON),
            str(ROOT / "tests/fixtures/probe_worker_multiparticle_split_helper.py"),
            str(project_path), str(gpu_scratch), str(gpu_path)], timeout=120)
        gpu = json.loads(gpu_path.read_text(encoding="utf-8")) if gpu_path.is_file() else None
        gpu_comparison_path = output / f"{name}.gpu-comparison-input.json"
        if gpu is not None:
            gpu_comparison_path.write_text(json.dumps(normalize_gpu(gpu), ensure_ascii=False,
                                                      allow_nan=False, indent=2) + "\n", encoding="utf-8")

        comparison_path = output / f"{name}.comparison.json"
        comparison = subprocess.run([str(PYTHON), str(ROOT / "tests/fixtures/compare_multiparticle_gpu_split.py"),
            str(project_path), str(cpu_path), str(gpu_comparison_path),
            str(output / f"{name}.cpu.guard.json"), str(output / f"{name}.gpu.guard.json"),
            str(comparison_path)], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False) \
            if cpu is not None and gpu is not None else None
        report = json.loads(comparison_path.read_text(encoding="utf-8")) if comparison_path.is_file() else {}
        metrics = report.get("cases", [{}])[0].get("comparisons", {})
        row = {
            "profile": name, "timeStepRatio": cfl, "raysPerPoint": rays,
            "cpuReturnCode": cpu_run.returncode, "gpuReturnCode": gpu_run.returncode,
            "comparisonReturnCode": comparison.returncode if comparison else None,
            "geometryErrorInGridSteps": metrics.get("errorInGridSteps"),
            "withinOneTenthGrid": metrics.get("withinOneTenthGrid"),
            "topologyMatchesDirect": metrics.get("topologyMatchesDirect"),
            "cellMaterialIdsMatchDirect": metrics.get("cellMaterialIdsMatchDirect"),
            "callbacksObservedForEverySpecies": metrics.get("callbacksObservedForEverySpecies"),
            "comparison": str(comparison_path.relative_to(ROOT)),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    passed = all(row["cpuReturnCode"] == 0 and row["gpuReturnCode"] == 0
                 and row["comparisonReturnCode"] == 0 for row in rows)
    summary = {
        "status": "completed" if passed else "incomplete",
        "scope": "2D trench two-ion etch; bounded direct CPU_DISK versus GPU_DISK operator-split profiles",
        "profiles": rows,
        "acceptancePolicy": "exploratory profile only; promotion requires all structural checks and <= 0.1 grid-step geometry error at two seeds plus UI acceptance",
    }
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "summary": str(summary_path.relative_to(ROOT))}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
