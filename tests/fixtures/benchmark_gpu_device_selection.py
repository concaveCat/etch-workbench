"""Controlled product-worker CPU_DISK vs RTX 3060 GPU_DISK performance audit."""
from __future__ import annotations

import importlib.metadata
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from backend.gpu_devices import cuda_devices, resolve_gpu_device
from backend.runtime_limits import available_cpu_threads
from run_gpu_singleparticle_coverage import make_generic_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/performance"
RUNS = ROOT / "var/gpu-device-selection-performance"
SAMPLES = int(os.environ.get("VIENNAPS_DEVICE_BENCHMARK_SAMPLES", "5"))
BOOTSTRAP_DRAWS = 20000
SIGNIFICANT_SPEEDUP = 1.5
MINIMUM_LOWER_CI = 1.2


def smi_query(fields: str) -> str:
    try:
        result = subprocess.run(["nvidia-smi", f"--query-gpu={fields}",
                                 "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else f"error: {result.stderr.strip()[:300]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {type(exc).__name__}: {exc}"


class Telemetry:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.rows: list[dict[str, Any]] = []
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,temperature.gpu,power.draw",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True,
                    timeout=3, check=False)
                if result.returncode == 0:
                    self.rows.append({"monotonic": time.monotonic(), "csv": result.stdout.strip()})
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.stop_event.wait(0.25)

    def __enter__(self) -> "Telemetry":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=4)


def run_guard(name: str, command: list[str], *, timeout: int = 300,
              env: dict[str, str] | None = None) -> dict[str, Any]:
    record = EVIDENCE / f"{name}.guard.json"
    log = EVIDENCE / f"{name}.log"
    record.unlink(missing_ok=True)
    log.unlink(missing_ok=True)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
         "--record", str(record), "--log-path", str(log), "--timeout-seconds", str(timeout),
         "--max-rss-mib", "1800", "--max-log-bytes", "2000000", "--", *command],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout + 15, check=False)
    guard = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
    if completed.returncode != 0 or guard.get("returnCode") != 0 or guard.get("remainingOwnedGroupMembers"):
        raise RuntimeError(f"bounded {name} failed: runner={completed.returncode}, guard={guard}, stderr={completed.stderr[-2000:]}")
    return guard


def project_for(engine: str, device: dict[str, Any], threads: int) -> dict[str, Any]:
    project = make_generic_project(3, "etch", "none")
    project["name"] = f"GPU device benchmark {engine}"
    # 160 × 160 lateral cells with 256 rays per surface point; identical
    # physical input and discretization for both engines.
    project["domain"].update(gridDelta=0.04, xExtent=6.4, yExtent=6.4)
    # Use an exposed, etchable plane so the acceptance workload verifies real
    # geometry evolution rather than spending its budget on an entirely
    # masked stack top.
    project["geometry"] = {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"}
    project["solver"].update(raysPerPoint=256, cpuThreads=threads,
                              enginePreference="gpu" if engine == "GPU_DISK" else "cpu")
    if engine == "GPU_DISK":
        project["solver"]["gpuDeviceId"] = device["uuid"]
    else:
        project["solver"].pop("gpuDeviceId", None)
    return project


def snapshot_digest(snapshot: dict[str, Any], dimension: int) -> dict[str, Any]:
    cells = snapshot["lines"] if dimension == 2 else snapshot["triangles"]
    material_ids = snapshot["cellData"]["materialIds"]
    import hashlib
    payload = json.dumps({"points": snapshot["points"], "cells": cells,
                          "materialIds": material_ids,
                          "materialMap": snapshot["materialIdsByLevelSetIndex"]},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "pointCount": len(snapshot["points"]),
            "cellCount": len(cells), "materialIds": sorted(set(material_ids)),
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_digest(snapshot: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    payload = json.dumps({"points": snapshot["points"], "cells": snapshot["cells"],
                          "materialIds": snapshot["materialIds"],
                          "materialMap": snapshot["materialIdsByLevelSetIndex"]},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return {"sha256": hashlib.sha256(payload).hexdigest(), "pointCount": len(snapshot["points"]),
            "cellCount": len(snapshot["cells"]), "materialIds": sorted(set(snapshot["materialIds"])),
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def create_input(run_dir: Path, project: dict[str, Any], engine: str,
                 device: dict[str, Any]) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, mode=0o700)
    step = project["steps"][0]
    config = {"kind": "simulate", "runId": run_dir.name, "project": project,
              "runSeed": project["solver"]["random"]["seed"],
              "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                   "viennals": importlib.metadata.version("viennals")},
              "stepEngines": {step["id"]: engine}, "customMaterials": []}
    if engine == "GPU_DISK":
        config["selectedGpuDevice"] = device
    input_path = run_dir / "input.json"
    input_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return input_path, run_dir / "worker-result.json"


def one_product_run(engine: str, index: int, project: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    name = f"{engine.lower()}-{index:02d}"
    run_dir = RUNS / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    input_path, result_path = create_input(run_dir, project, engine, device)
    with Telemetry() as telemetry:
        started = time.monotonic()
        guard = run_guard(name, [str(ROOT / ".venv/bin/python"), str(ROOT / "worker/simulate.py"),
                                 str(input_path), str(run_dir)])
        wall_seconds = time.monotonic() - started
    worker = json.loads(result_path.read_text(encoding="utf-8"))
    step = worker["stepRecords"][0]
    elapsed = float(step["actualParameters"]["processApplySeconds"])
    initial_meta, final_meta = worker["snapshots"][0], worker["snapshots"][-1]
    initial_path, final_path = run_dir / initial_meta["snapshot"], run_dir / final_meta["snapshot"]
    signature = {"initial": snapshot_digest(json.loads(initial_path.read_text()), 3),
                 "final": snapshot_digest(json.loads(final_path.read_text()), 3)}
    if signature["initial"]["sha256"] == signature["final"]["sha256"]:
        raise RuntimeError("performance workload did not change the native surface geometry")
    if step["actualEngine"] != engine:
        raise RuntimeError(f"worker executed {step['actualEngine']} instead of {engine}")
    if engine == "GPU_DISK":
        readback = step["actualParameters"].get("cudaDeviceReadback", {})
        after = readback.get("afterProcessApply", {})
        process_visibility = readback.get("process", {}).get("processListStatus")
        if (readback.get("uuid") != device["uuid"] or readback.get("pciBusId") != "0000:01:00.0"
                or readback.get("synchronizeStatus") != 0
                or after.get("uuid") != device["uuid"] or after.get("pciBusId") != "0000:01:00.0"
                or after.get("synchronizeStatus") != 0 or after.get("readbackStatus") != "verified"
                or process_visibility not in {"verified", "not-observed", "unavailable"}
                or (process_visibility == "verified" and readback.get("process", {}).get("gpuUuid") != device["uuid"])):
            raise RuntimeError(f"GPU product did not run on selected RTX 3060: {readback}")
    sample = {"engine": engine, "index": index, "processApplySeconds": elapsed,
              "workerWallSeconds": wall_seconds, "peakObservedRssBytes": guard["peakObservedRssBytes"],
              "cpuThreads": worker["cpuThreads"], "actualEngine": step["actualEngine"],
              "cudaDeviceReadback": step["actualParameters"].get("cudaDeviceReadback"),
              "outputDigests": signature, "telemetrySamples": telemetry.rows,
              "guard": str((EVIDENCE / f"{name}.guard.json").relative_to(ROOT)),
              "log": str((EVIDENCE / f"{name}.log").relative_to(ROOT)),
              "productArtifact": None}
    if index == 1:
        archive_dir = EVIDENCE / f"{engine.lower()}-sample-01"
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
        archive_dir.mkdir(parents=True)
        for source in (input_path, result_path, initial_path, final_path,
                       run_dir / initial_meta["vtk"], run_dir / final_meta["vtk"]):
            shutil.copy2(source, archive_dir / source.name)
        sample["productArtifact"] = str(archive_dir.relative_to(ROOT))
    shutil.rmtree(run_dir)
    return sample


def direct_reference(engine: str, project: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    project_path = EVIDENCE / "benchmark-project.json"
    direct_path = EVIDENCE / f"{engine.lower()}-direct-reference.json"
    direct_env = dict(os.environ)
    if engine == "GPU_DISK":
        direct_env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        direct_env["CUDA_VISIBLE_DEVICES"] = device["uuid"]
    else:
        direct_env.pop("CUDA_VISIBLE_DEVICES", None)
    guard = run_guard(f"{engine.lower()}-direct", [str(ROOT / ".venv/bin/python"),
                     str(ROOT / "tests/fixtures/gpu_singleparticle_direct_reference.py"),
                     str(project_path), str(direct_path), engine], env=direct_env)
    result = json.loads(direct_path.read_text(encoding="utf-8"))
    result_signatures = {"initial": direct_digest(result["initial"]),
                         "final": direct_digest(result["final"])}
    return {"engine": result["engine"], "elapsedSeconds": result["elapsedSeconds"],
            "threads": result["threads"], "outputDigests": result_signatures,
            "outputPath": str(direct_path.relative_to(ROOT)),
            "guard": str((EVIDENCE / f"{engine.lower()}-direct.guard.json").relative_to(ROOT)),
            "reference": str(direct_path.relative_to(ROOT)),
            "peakObservedRssBytes": guard["peakObservedRssBytes"]}


def directed_vertex_distance(source: list[list[float]], target: list[list[float]],
                             grid_delta: float) -> float:
    """Return point-cloud directed Hausdorff distance using spatial buckets."""
    buckets: dict[tuple[int, int, int], list[list[float]]] = {}
    for point in target:
        key = tuple(math.floor(float(value) / grid_delta) for value in point)
        buckets.setdefault(key, []).append(point)
    maximum = 0.0
    for point in source:
        key = tuple(math.floor(float(value) / grid_delta) for value in point)
        nearest = float("inf")
        for i in range(key[0] - 2, key[0] + 3):
            for j in range(key[1] - 2, key[1] + 3):
                for k in range(key[2] - 2, key[2] + 3):
                    for other in buckets.get((i, j, k), ()):
                        distance = math.dist(point, other)
                        if distance < nearest:
                            nearest = distance
        if not math.isfinite(nearest):
            raise RuntimeError("cross-engine surface comparison found no nearby counterpart vertex")
        maximum = max(maximum, nearest)
    return maximum


def compare_engine_references(references: dict[str, Any], grid_delta: float) -> dict[str, Any]:
    gpu_path = ROOT / references["GPU_DISK"]["outputPath"]
    cpu_path = ROOT / references["CPU_DISK"]["outputPath"]
    gpu = json.loads(gpu_path.read_text(encoding="utf-8"))
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    initial_exact = references["GPU_DISK"]["outputDigests"]["initial"] == references["CPU_DISK"]["outputDigests"]["initial"]
    gpu_final, cpu_final = gpu["final"], cpu["final"]
    maximum_vertex_distance = max(
        directed_vertex_distance(gpu_final["points"], cpu_final["points"], grid_delta),
        directed_vertex_distance(cpu_final["points"], gpu_final["points"], grid_delta),
    )
    same_material_mapping = gpu_final["materialIdsByLevelSetIndex"] == cpu_final["materialIdsByLevelSetIndex"]
    same_material_set = set(gpu_final["materialIds"]) == set(cpu_final["materialIds"])
    return {
        "initialGeometryExactlyEqual": initial_exact,
        "finalTopologyExactlyEqual": gpu_final["cells"] == cpu_final["cells"],
        "finalMaterialCellArrayExactlyEqual": gpu_final["materialIds"] == cpu_final["materialIds"],
        "finalMaterialSetEqual": same_material_set,
        "finalMaterialMappingEqual": same_material_mapping,
        "finalPointCountGpu": len(gpu_final["points"]),
        "finalPointCountCpu": len(cpu_final["points"]),
        "finalCellCountGpu": len(gpu_final["cells"]),
        "finalCellCountCpu": len(cpu_final["cells"]),
        "symmetricVertexDistanceUm": maximum_vertex_distance,
        "gridDeltaUm": grid_delta,
        "withinOneGridSpacing": maximum_vertex_distance <= grid_delta,
        "metric": "maximum symmetric nearest-vertex distance between the native GPU and CPU final surface point clouds; topology may differ",
    }


def median_ratio_ci(cpu: list[float], gpu: list[float]) -> tuple[float, list[float]]:
    rng = random.Random(3060)
    ratios = []
    for _ in range(BOOTSTRAP_DRAWS):
        cpu_sample = [rng.choice(cpu) for _ in cpu]
        gpu_sample = [rng.choice(gpu) for _ in gpu]
        ratios.append(statistics.median(cpu_sample) / statistics.median(gpu_sample))
    ratios.sort()
    return statistics.median(cpu) / statistics.median(gpu), [ratios[int(BOOTSTRAP_DRAWS * 0.025)],
                                                              ratios[int(BOOTSTRAP_DRAWS * 0.975)]]


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    device = resolve_gpu_device(None, cuda_devices(refresh=True))
    threads = available_cpu_threads()
    gpu_project = project_for("GPU_DISK", device, threads)
    cpu_project = project_for("CPU_DISK", device, threads)
    (EVIDENCE / "benchmark-project.json").write_text(json.dumps(gpu_project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Independent native references are run once per engine before timed samples.
    references = {engine: direct_reference(engine, project, device)
                  for engine, project in (("GPU_DISK", gpu_project), ("CPU_DISK", cpu_project))}
    cross_engine = compare_engine_references(references, float(gpu_project["domain"]["gridDelta"]))
    samples: dict[str, list[dict[str, Any]]] = {"GPU_DISK": [], "CPU_DISK": []}
    # One untimed warm-up per engine on the exact measured workload.
    warmups = []
    for engine, project in (("GPU_DISK", gpu_project), ("CPU_DISK", cpu_project)):
        warmups.append(one_product_run(engine, 0, project, device))
    # Paired, sequential measurements; the two native engines never overlap.
    for index in range(1, SAMPLES + 1):
        for engine, project in (("GPU_DISK", gpu_project), ("CPU_DISK", cpu_project)):
            sample = one_product_run(engine, index, project, device)
            expected = references[engine]["outputDigests"]
            sample["matchesIndependentDirectReference"] = sample["outputDigests"] == expected
            if not sample["matchesIndependentDirectReference"]:
                raise RuntimeError(f"{engine} sample {index} differs from the independent ViennaPS reference")
            samples[engine].append(sample)
    gpu_times = [row["processApplySeconds"] for row in samples["GPU_DISK"]]
    cpu_times = [row["processApplySeconds"] for row in samples["CPU_DISK"]]
    speedup, ci = median_ratio_ci(cpu_times, gpu_times)
    passed = (speedup >= SIGNIFICANT_SPEEDUP and ci[0] > MINIMUM_LOWER_CI
              and cross_engine["initialGeometryExactlyEqual"]
              and cross_engine["finalMaterialSetEqual"]
              and cross_engine["finalMaterialMappingEqual"]
              and cross_engine["withinOneGridSpacing"])
    report = {
        "status": "passed" if passed else "insufficient-speedup",
        "host": {"selectedDevice": device, "visibleCudaDevices": 1,
                 "installedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                       "viennals": importlib.metadata.version("viennals")},
                 "cpuThreads": threads},
        "workload": {"dimension": 3, "model": "singleParticle", "operation": "etch",
                     "geometry": "exposed Si plane; surface evolution required",
                     "gridDelta": gpu_project["domain"]["gridDelta"],
                     "xExtent": gpu_project["domain"]["xExtent"],
                     "yExtent": gpu_project["domain"]["yExtent"],
                     "lateralGridCells": [round(gpu_project["domain"]["xExtent"] / gpu_project["domain"]["gridDelta"]),
                                           round(gpu_project["domain"]["yExtent"] / gpu_project["domain"]["gridDelta"])],
                     "raysPerPoint": gpu_project["solver"]["raysPerPoint"],
                     "fixedSeed": gpu_project["solver"]["random"]["seed"],
                     "timeStepRatio": gpu_project["solver"]["timeStepRatio"],
                     "duration": gpu_project["steps"][0]["duration"],
                     "cpuThreadsForBothEngines": threads,
                     "difference": "only concrete flux engine and selected GPU UUID; same domain, geometry, seed, rates, discretization and thread count"},
        "method": {"warmupsPerEngine": 1, "measuredRunsPerEngine": SAMPLES,
                   "order": "paired sequential GPU_DISK then CPU_DISK; no overlap",
                   "timedRegion": "worker perf_counter around ViennaPS Process.apply only",
                   "summaryStatistic": "median",
                   "significanceThreshold": {"medianSpeedupAtLeast": SIGNIFICANT_SPEEDUP,
                                             "bootstrapLower95PercentileAbove": MINIMUM_LOWER_CI,
                                             "bootstrapDraws": BOOTSTRAP_DRAWS,
                                             "confidenceInterval": "percentile bootstrap, seed 3060"}},
        "independentDirectReferences": references,
        "warmups": warmups,
        "measurements": samples,
        "performance": {"gpuMedianSeconds": statistics.median(gpu_times),
                        "cpuMedianSeconds": statistics.median(cpu_times),
                        "medianSpeedupCpuOverGpu": speedup,
                        "bootstrap95PercentileSpeedupInterval": ci,
                        "gpuTimesSeconds": gpu_times, "cpuTimesSeconds": cpu_times},
        "acceptanceComparisons": {"eachEngineMatchesIndependentDirectReference": True,
                                  "crossEngineFinalSurface": cross_engine,
                                  "bothEnginesChangedSurfaceGeometry": all(
                                      row["outputDigests"]["initial"]["sha256"] != row["outputDigests"]["final"]["sha256"]
                                      for engine_rows in samples.values() for row in engine_rows),
                                  "gpuWorkerCudaUuid": device["uuid"],
                                  "gpuWorkerCudaDeviceVerifiedAfterApply": all(
                                      row["actualEngine"] == "GPU_DISK"
                                      and row["cudaDeviceReadback"]["uuid"] == device["uuid"]
                                      and row["cudaDeviceReadback"]["afterProcessApply"]["uuid"] == device["uuid"]
                                      and row["cudaDeviceReadback"]["afterProcessApply"]["synchronizeStatus"] == 0
                                      for row in samples["GPU_DISK"]),
                                  "nvidiaSmiProcessVisibility": [
                                      row["cudaDeviceReadback"]["process"]["processListStatus"]
                                      for row in samples["GPU_DISK"]],
                                  "cpuWorkerEngine": all(row["actualEngine"] == "CPU_DISK" for row in samples["CPU_DISK"])},
        "evidencePolicy": "GPU_DISK stays pinned without fallback; exact native output, selected PCI/UUID, post-apply CUDA synchronization, and paired performance are acceptance evidence. nvidia-smi PID enumeration is recorded as advisory because a bounded active CUDA-allocation probe on this host is also absent from that process table. Only sample 1 product snapshots are copied in full; every measured sample retains its bounded-child guard/log and exact output digests.",
    }
    (EVIDENCE / "benchmark-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "gpuMedianSeconds": report["performance"]["gpuMedianSeconds"],
                      "cpuMedianSeconds": report["performance"]["cpuMedianSeconds"],
                      "medianSpeedup": speedup, "ci95": ci, "crossEngineVertexDistanceUm": cross_engine["symmetricVertexDistanceUm"],
                      "threads": threads}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
