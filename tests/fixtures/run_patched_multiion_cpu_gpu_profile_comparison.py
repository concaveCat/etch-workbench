#!/usr/bin/env python3
"""Compare the repaired native MultiParticle CPU_DISK and GPU_DISK routes.

Every product and direct native execution is launched by
run_gpu_geometry_coverage.py through scripts/bounded_child.py.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import subprocess
import time
from pathlib import Path
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/fixtures/run_gpu_geometry_coverage.py"
PRESETS = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets"
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/profile-recheck"
CASES = [
    (2, 0.25, 64, None),
    (2, 0.25, 512, None),
    (3, 0.25, 64, 0.6),
    (3, 0.25, 256, 0.6),
    (3, 0.20, 1024, 0.6),
    (3, 0.25, 1024, 0.6),
]
EXPECTED_COMPARISONS = {
    "initialPoints", "initialCells", "initialMaterialIds", "initialMaterialMap",
    "finalPoints", "finalCells", "finalMaterialIds", "finalMaterialMap",
    "productEngine", "directEngine", "versions", "multiParticleSpeciesConfiguration",
    "rateCallbackCount", "rateCallbackMaterials", "rateCallbackFluxSums",
    "rateCallbackFluxMaxima", "rateCallbackMeanFlux", "rateCallbackFormulaSamples",
}


def acceptance_path(dimension: int, cfl: float, rays: int,
                    mask_height: float | None, engine: str) -> Path:
    mask = f"-mask-{mask_height:g}" if mask_height is not None else ""
    name = (f"multiParticle-{dimension}d-trench-direct{mask}-cfl{cfl:g}-rays{rays}"
            f"-species-two-ion-different-energy-etch-{engine.lower()}")
    return PRESETS / name / "acceptance.json"


def nearest_metrics(source: list[list[float]], target: list[list[float]],
                    delta: float) -> dict[str, Any]:
    """Return exact nearest-vertex distances up to a 2-cell search cutoff."""
    cutoff = 2.0 * delta
    tolerance = 0.1 * delta
    ordered = sorted((float(point[0]), point) for point in target)
    xs = [row[0] for row in ordered]
    distances: list[float] = []
    beyond_cutoff = 0
    for point in source:
        x = float(point[0])
        left = bisect.bisect_left(xs, x - cutoff)
        right = bisect.bisect_right(xs, x + cutoff)
        nearest_squared = math.inf
        for _, candidate in ordered[left:right]:
            squared = sum((float(a) - float(b)) ** 2 for a, b in zip(point, candidate))
            if squared < nearest_squared:
                nearest_squared = squared
        if nearest_squared == math.inf or nearest_squared > cutoff * cutoff:
            beyond_cutoff += 1
        else:
            distances.append(math.sqrt(nearest_squared))
    return {
        "sourcePointCount": len(source), "targetPointCount": len(target),
        "cutoffUm": cutoff, "toleranceUm": tolerance,
        "pointsBeyondCutoff": beyond_cutoff,
        "maxNearestDistanceWithinCutoffUm": max(distances, default=None),
        "meanNearestDistanceWithinCutoffUm": sum(distances) / len(distances) if distances else None,
        "allPointsWithinOneTenthGrid": beyond_cutoff == 0 and len(distances) == len(source)
        and max(distances, default=math.inf) <= tolerance,
    }


def run_cpu_case(dimension: int, cfl: float, rays: int, mask_height: float | None,
                 env: dict[str, str], reuse: bool) -> dict[str, Any]:
    path = acceptance_path(dimension, cfl, rays, mask_height, "CPU_DISK")
    command = [
        str(ROOT / ".venv/bin/python"), str(RUNNER), "--geometry", "trench",
        "--model", "multiParticle", "--operation", "etch", "--dimension", str(dimension),
        "--engine", "CPU_DISK", "--species-profile", "two-ion-different-energy",
        "--time-step-ratio", str(cfl), "--rays-per-point", str(rays),
    ]
    if mask_height is not None:
        command += ["--mask-height", str(mask_height)]
    case = f"{dimension}d-trench-cfl{cfl:g}-rays{rays}"
    proc_returncode = 0
    if not reuse:
        proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=600, check=False)
        (EVIDENCE / f"cpu-{case}.stdout.log").write_text(proc.stdout, encoding="utf-8")
        (EVIDENCE / f"cpu-{case}.stderr.log").write_text(proc.stderr, encoding="utf-8")
        proc_returncode = proc.returncode
    try:
        acceptance = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"case": case, "engine": "CPU_DISK", "status": "failed",
                "returnCode": proc_returncode, "error": str(exc)}
    comparisons = acceptance.get("comparisons", {})
    passed = (proc_returncode == 0 and acceptance.get("status") == "verified"
              and EXPECTED_COMPARISONS.issubset(comparisons)
              and all(comparisons[key] is True for key in EXPECTED_COMPARISONS))
    failure_excerpts = {}
    for engine_key in ("productLog", "directLog"):
        log_path = ROOT / acceptance.get(engine_key, "")
        if not passed and log_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")
            errors = re.findall(r"(?:RuntimeError|ValueError|TypeError|FloatingPointError):[^\n]*", log)
            failure_excerpts[engine_key.removesuffix("Log")] = errors[-1] if errors else log[-600:]
    return {"case": case, "engine": "CPU_DISK", "status": "verified" if passed else "failed",
            "returnCode": proc_returncode, "nativeStatus": acceptance.get("status"),
            "acceptancePath": str(path.relative_to(ROOT)), "comparisons": comparisons,
            "productGuard": acceptance.get("productGuard"),
            "directGuard": acceptance.get("directGuard"),
            "failureExcerpts": failure_excerpts, "command": command}


def direct_data(acceptance: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / acceptance["directReference"]
    return json.loads(path.read_text(encoding="utf-8"))


def compare_engines(dimension: int, cfl: float, rays: int, mask_height: float | None,
                    cpu_acceptance: dict[str, Any], gpu_acceptance: dict[str, Any]) -> dict[str, Any]:
    cpu = direct_data(cpu_acceptance)
    gpu = direct_data(gpu_acceptance)
    project = json.loads((ROOT / gpu_acceptance["project"]).read_text(encoding="utf-8"))
    delta = float(project["domain"]["gridDelta"])
    results: dict[str, Any] = {}
    for when in ("initial", "final"):
        left, right = cpu[when], gpu[when]
        results[when] = {
            "cpuPointCount": len(left["points"]), "gpuPointCount": len(right["points"]),
            "cpuCellCount": len(left["cells"]), "gpuCellCount": len(right["cells"]),
            "pointsExactlyEqual": left["points"] == right["points"],
            "connectivityExactlyEqual": left["cells"] == right["cells"],
            "cellMaterialIdsExactlyEqual": left["materialIds"] == right["materialIds"],
            "levelSetMaterialMapExactlyEqual": left["materialIdsByLevelSetIndex"] == right["materialIdsByLevelSetIndex"],
            "cpuToGpuNearestVertices": nearest_metrics(left["points"], right["points"], delta),
            "gpuToCpuNearestVertices": nearest_metrics(right["points"], left["points"], delta),
        }
    cpu_flux = cpu.get("callback") or {}
    gpu_flux = gpu.get("callback") or {}
    cpu_means = cpu_flux.get("meanFluxBySpecies", [])
    gpu_means = gpu_flux.get("meanFluxBySpecies", [])
    species_order = cpu.get("actualParameters", {}).get("speciesOrder", [])
    flux_comparison = []
    if len(cpu_means) == len(gpu_means) == len(species_order):
        for label, cpu_mean, gpu_mean in zip(species_order, cpu_means, gpu_means):
            cpu_mean, gpu_mean = float(cpu_mean), float(gpu_mean)
            flux_comparison.append({
                "species": label, "cpuMeanFlux": cpu_mean, "gpuMeanFlux": gpu_mean,
                "relativeGpuMinusCpuPercent": (100.0 * (gpu_mean - cpu_mean) / abs(cpu_mean)
                                                if cpu_mean else None),
            })
    else:
        flux_comparison.append({"error": "CPU and GPU callback species vectors differ"})
    return {
        "dimension": dimension, "geometry": "trench", "maskHeight": mask_height,
        "timeStepRatio": cfl, "raysPerPoint": rays, "gridDelta": delta,
        "cpuActualEngine": cpu.get("actualEngine"), "gpuActualEngine": gpu.get("actualEngine"),
        "speciesOrder": species_order, "surfaceComparisons": results,
        "fluxMeanComparisons": flux_comparison,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse-cpu", action="store_true",
                        help="reuse existing CPU_DISK acceptance files instead of rerunning native cases")
    cli = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("VIENNAPS_PATCHED_RUNTIME_DIR", "/tmp/viennaps-normal-fix-runtime")
    cpu_runs = []
    comparisons = []
    for dimension, cfl, rays, mask_height in CASES:
        cpu_run = run_cpu_case(dimension, cfl, rays, mask_height, env, cli.reuse_cpu)
        cpu_runs.append(cpu_run)
        if cpu_run.get("status") != "verified":
            continue
        cpu_acceptance = json.loads((ROOT / cpu_run["acceptancePath"]).read_text(encoding="utf-8"))
        gpu_path = acceptance_path(dimension, cfl, rays, mask_height, "GPU_DISK")
        try:
            gpu_acceptance = json.loads(gpu_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if gpu_acceptance.get("status") != "verified":
            continue
        comparisons.append(compare_engines(dimension, cfl, rays, mask_height,
                                           cpu_acceptance, gpu_acceptance))
    failed_cpu_cases = [row for row in cpu_runs if row.get("status") != "verified"]
    summary_status = ("passed" if len(comparisons) == len(CASES) and not failed_cpu_cases
                      else "partial" if comparisons else "failed")
    summary = {
        "schemaVersion": 1,
        "status": summary_status,
        "description": "Same-profile direct ViennaPS CPU_DISK versus patched-runtime RTX 3060 GPU_DISK comparison. Product-worker/direct parity is checked per engine; cross-engine meshes and means are reported without requiring exact CPU/GPU equality.",
        "deviceUuid": "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
        "runtimeDirectory": env["VIENNAPS_PATCHED_RUNTIME_DIR"],
        "cpuRuns": cpu_runs, "comparisons": comparisons,
        "caseCount": len(comparisons), "expectedCaseCount": len(CASES),
        "failedCpuCaseCount": len(failed_cpu_cases),
        "interpretation": "A CPU_DISK direct and product failure is recorded as a native CPU issue, not a GPU fallback or WebUI/GPU mismatch. Successful cross-engine comparisons report numerical and mesh differences without imposing exact CPU/GPU equality.",
    }
    debug_log = EVIDENCE / "cpu-nan-debug/worker.log"
    debug_guard = EVIDENCE / "cpu-nan-debug/worker.guard.json"
    if debug_log.is_file() and debug_guard.is_file():
        log_text = debug_log.read_text(encoding="utf-8", errors="replace")
        guard = json.loads(debug_guard.read_text(encoding="utf-8"))
        errors = re.findall(r"RuntimeError:([^\n]*)", log_text)
        summary["cpuFailureWorkerRerun"] = {
            "status": "reproduced" if guard.get("returnCode") == 1 and any(
                "non-finite species flux" in error for error in errors) else "unexpected",
            "runtimeError": errors[-1].strip() if errors else None,
            "returnCode": guard.get("returnCode"), "timedOut": guard.get("timedOut"),
            "rssLimited": guard.get("rssLimited"),
            "remainingOwnedGroupMembers": guard.get("remainingOwnedGroupMembers"),
            "log": str(debug_log.relative_to(ROOT)), "guard": str(debug_guard.relative_to(ROOT)),
        }
    path = EVIDENCE / "cpu-gpu-profile-comparison.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "caseCount": summary["caseCount"],
                      "expectedCaseCount": summary["expectedCaseCount"],
                      "evidence": str(path.relative_to(ROOT))}, ensure_ascii=False))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
