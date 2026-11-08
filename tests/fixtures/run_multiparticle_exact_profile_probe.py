"""Bounded direct CPU/GPU comparison for one MultiParticle solver profile.

This runner records exploratory evidence only. It never promotes capability
matrix rows; a passing profile still needs product-browser acceptance.
Every ViennaPS invocation is started beneath scripts/bounded_child.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from run_multiparticle_split_profile_sweep import normalize_cpu, normalize_gpu, run_bounded  # noqa: E402


def under_root(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"probe evidence must be stored beneath {ROOT}")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cfl", type=float, required=True)
    parser.add_argument("--rays", type=int, required=True)
    parser.add_argument("--seed", type=int, default=314159)
    args = parser.parse_args()

    source = under_root(args.project)
    output = under_root(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    base: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    if not any(step.get("enabled") and step.get("model") == "multiParticle"
               for step in base.get("steps", [])):
        raise SystemExit("source project must contain an enabled MultiParticle step")
    if args.cfl <= 0 or args.rays <= 0:
        raise SystemExit("CFL and ray count must be positive")

    project = json.loads(json.dumps(base))
    project["name"] = f"Exploratory exact GPU profile {args.cfl} / {args.rays} rays / seed {args.seed}"
    project["solver"]["timeStepRatio"] = args.cfl
    project["solver"]["raysPerPoint"] = args.rays
    project["solver"]["random"] = {"mode": "fixed", "seed": args.seed}
    project_path = output / "project.json"
    project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cpu_raw_path = output / "cpu-direct-raw.json"
    cpu_scratch = output / "cpu-scratch"
    cpu_scratch.mkdir(parents=True, exist_ok=True)
    cpu = run_bounded(output, "cpu-direct", [str(PYTHON),
        str(ROOT / "tests/fixtures/gpu_geometry_direct_reference.py"),
        str(project_path), str(cpu_raw_path), str(cpu_scratch), "CPU_DISK"])
    cpu_raw = json.loads(cpu_raw_path.read_text(encoding="utf-8")) if cpu_raw_path.is_file() else None
    if cpu_raw is not None:
        (output / "cpu-reference.json").write_text(
            json.dumps(normalize_cpu(cpu_raw, project), ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8")

    gpu_raw_path = output / "gpu-split.json"
    gpu_scratch = output / "gpu-scratch"
    gpu_scratch.mkdir(parents=True, exist_ok=True)
    gpu = run_bounded(output, "gpu-split", [str(PYTHON),
        str(ROOT / "tests/fixtures/probe_worker_multiparticle_split_helper.py"),
        str(project_path), str(gpu_scratch), str(gpu_raw_path)], timeout=120)
    gpu_raw = json.loads(gpu_raw_path.read_text(encoding="utf-8")) if gpu_raw_path.is_file() else None
    if gpu_raw is not None:
        (output / "gpu-comparison-input.json").write_text(
            json.dumps(normalize_gpu(gpu_raw), ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8")

    comparison_path = output / "comparison.json"
    compare: subprocess.CompletedProcess[str] | None = None
    if cpu_raw is not None and gpu_raw is not None:
        compare = subprocess.run([str(PYTHON), str(ROOT / "tests/fixtures/compare_multiparticle_gpu_split.py"),
            str(project_path), str(output / "cpu-reference.json"), str(output / "gpu-comparison-input.json"),
            str(output / "cpu-direct.guard.json"), str(output / "gpu-split.guard.json"), str(comparison_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    comparison = json.loads(comparison_path.read_text(encoding="utf-8")) if comparison_path.is_file() else {}
    guard_rows = {}
    for name in ("cpu-direct", "gpu-split"):
        path = output / f"{name}.guard.json"
        guard_rows[name] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    checks = comparison.get("cases", [{}])[0].get("comparisons", {})
    accepted_by_direct_checks = bool(
        cpu.returncode == 0 and gpu.returncode == 0 and compare and compare.returncode == 0
        and comparison.get("initialMatchesDirect")
        and all(comparison.get("guards", {}).values())
        and all(checks.get(key) is True for key in (
            "topologyMatchesDirect", "cellMaterialIdsMatchDirect", "levelSetMaterialMapMatchesDirect",
            "levelSetCountMatchesDirect", "withinOneTenthGrid", "callbacksObservedForEverySpecies")))
    report = {
        "status": "promising-direct-only" if accepted_by_direct_checks else "failed-direct-checks",
        "promotionStatus": "not-promoted; product UI acceptance is still required",
        "dimension": project["dimension"], "geometry": project["geometry"]["kind"],
        "operation": next(step["operation"] for step in project["steps"] if step.get("enabled")),
        "timeStepRatio": args.cfl, "raysPerPoint": args.rays, "seed": args.seed,
        "cpuReturnCode": cpu.returncode, "gpuReturnCode": gpu.returncode,
        "comparisonReturnCode": compare.returncode if compare else None,
        "device": gpu_raw.get("device") if gpu_raw else None,
        "comparisons": checks, "guards": guard_rows,
        "project": str(project_path.relative_to(ROOT)),
        "cpuReference": str((output / "cpu-reference.json").relative_to(ROOT)),
        "gpuOutput": str(gpu_raw_path.relative_to(ROOT)),
        "comparison": str(comparison_path.relative_to(ROOT)) if comparison_path.is_file() else None,
    }
    (output / "probe-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "profile": [args.cfl, args.rays],
                      "errorInGridSteps": checks.get("errorInGridSteps"),
                      "topologyMatchesDirect": checks.get("topologyMatchesDirect"),
                      "cellMaterialIdsMatchDirect": checks.get("cellMaterialIdsMatchDirect"),
                      "deviceUuid": (report.get("device") or {}).get("uuid"),
                      "output": str(output.relative_to(ROOT))}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "promising-direct-only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
