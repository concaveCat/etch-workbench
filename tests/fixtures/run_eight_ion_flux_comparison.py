"""Compare 3D eight-ion calculateFlux() output on a shared saved state."""
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
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--rays", type=int, default=64)
    parser.add_argument("--seed", type=int, default=314159)
    args = parser.parse_args()
    runtime = args.runtime_dir.resolve(strict=True)
    project = args.project.resolve(strict=True)
    state = args.state.resolve(strict=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report_path = EVIDENCE / "eight-ion-calculate-flux-3d-comparison.json"
    report: dict[str, object] = {"status": "running", "runtimeDir": str(runtime), "state": str(state),
                                 "deviceUuid": DEVICE_UUID, "rays": args.rays, "seed": args.seed,
                                 "cases": []}

    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(runtime), str(ROOT), env.get("PYTHONPATH", "")])
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = DEVICE_UUID
    outputs = {}
    try:
        for engine in ("CPU_DISK", "GPU_DISK"):
            stem = f"eight-ion-calculate-flux-3d-{engine.lower()}"
            output, guard, log = (EVIDENCE / f"{stem}.{suffix}" for suffix in ("json", "guard.json", "log"))
            command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                       "--record", str(guard), "--log-path", str(log), "--parent-pid", str(os.getpid()),
                       "--timeout-seconds", "120", "--max-rss-mib", "2200", "--max-log-bytes", "2000000",
                       "--", sys.executable, str(ROOT / "tests/fixtures/flux3d_reference_full.py"),
                       "--project", str(project), "--state", str(state), "--engine", engine,
                       "--rays", str(args.rays), "--seed", str(args.seed), "--out", str(output)]
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                                       timeout=145, check=False)
            row = {"engine": engine, "returnCode": completed.returncode,
                   "output": str(output.relative_to(ROOT)), "guard": str(guard.relative_to(ROOT)),
                   "log": str(log.relative_to(ROOT))}
            report["cases"].append(row)  # type: ignore[union-attr]
            save()
            if completed.returncode != 0:
                raise RuntimeError(f"{engine} calculateFlux failed; inspect {log}")
            data = json.loads(output.read_text(encoding="utf-8"))
            if not Path(data["viennapsModulePath"]).resolve().is_relative_to(runtime):
                raise RuntimeError(f"{engine} loaded a ViennaPS package outside the candidate runtime")
            if data["engine"] != engine or len(data["species"]) != 8:
                raise RuntimeError(f"{engine} returned an unexpected engine or species count")
            fields = {key: value for key, value in data.items()
                      if isinstance(value, dict) and "values" in value
                      and key not in {"LSValues", "MaterialIds"}}
            expected_fields = {f"{label}{index}" for index, label in enumerate(data["species"])}
            if set(fields) != expected_fields or any(value["nonfinite"] for value in fields.values()):
                raise RuntimeError(f"{engine} returned missing or non-finite species fields")
            row["species"] = data["species"]
            row["fieldNames"] = sorted(fields)
            row["fieldSampleCounts"] = {key: len(value["values"]) for key, value in fields.items()}
            outputs[engine] = (data, fields)
            save()

        cpu, cpu_fields = outputs["CPU_DISK"]
        gpu, gpu_fields = outputs["GPU_DISK"]
        if cpu["points"] != gpu["points"] or cpu["cells"] != gpu["cells"]:
            raise RuntimeError("CPU/GPU calculateFlux meshes are not pointwise aligned")
        if set(cpu_fields) != set(gpu_fields):
            raise RuntimeError("CPU/GPU calculateFlux species field labels differ")
        comparisons = {}
        for name in sorted(cpu_fields):
            left, right = cpu_fields[name]["values"], gpu_fields[name]["values"]
            if len(left) != len(right):
                raise RuntimeError(f"{name} flux sample counts differ")
            mean_cpu = sum(left) / len(left) if left else 0.0
            mean_gpu = sum(right) / len(right) if right else 0.0
            abs_relative = [abs(a - b) / max(abs(a), abs(b), 1e-30) for a, b in zip(left, right)]
            comparisons[name] = {
                "samples": len(left),
                "meanCPU": mean_cpu,
                "meanGPU": mean_gpu,
                "relativeMeanDifference": abs(mean_gpu - mean_cpu) / max(abs(mean_cpu), 1e-30),
                "meanAbsolutePointwiseRelativeDifference": sum(abs_relative) / len(abs_relative)
                    if abs_relative else 0.0,
                "maxAbsolutePointwiseRelativeDifference": max(abs_relative, default=0.0),
                "cpuNonfinite": cpu_fields[name]["nonfinite"],
                "gpuNonfinite": gpu_fields[name]["nonfinite"],
            }
        report["mesh"] = {"samePoints": True, "sameCells": True,
                           "pointCount": len(cpu["points"]), "cellCount": len(cpu["cells"])}
        report["fields"] = comparisons
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
