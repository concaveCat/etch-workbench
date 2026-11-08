"""Run bounded, independent GPU checks against an isolated ViennaPS build."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-fix-2026-10-28"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--device-uuid", required=True)
    parser.add_argument("--check-empty-map", action="store_true")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.resolve(strict=True)
    if not (runtime_dir / "viennaps").is_dir():
        raise ValueError("runtime directory must contain the candidate viennaps package")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report_path = EVIDENCE / "candidate-native-acceptance.json"
    report = {"status": "running", "runtimeDir": str(runtime_dir), "deviceUuid": args.device_uuid,
              "cases": [], "parameterChecks": []}

    def save():
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = args.device_uuid
    env["PYTHONPATH"] = os.pathsep.join([str(runtime_dir), str(ROOT), env.get("PYTHONPATH", "")])
    variants = {"baseline": [], "suppress-xenon": ["--xenon-threshold", "10000"],
                "suppress-argon": ["--argon-threshold", "10000"]}
    try:
        for dimension in (2, 3):
            project = (ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets" /
                       f"multiParticle-{dimension}d-plane-direct-species-two-ion-different-energy-etch-gpu_disk/project.json")
            for route in ("generic", "gpu-specific"):
                outputs = {}
                for variant, extra in variants.items():
                    name = f"candidate-{dimension}d-{route}-{variant}"
                    output, guard, log = (EVIDENCE / f"{name}.{suffix}" for suffix in ("json", "guard.json", "log"))
                    command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                               "--record", str(guard), "--log-path", str(log), "--parent-pid", str(os.getpid()),
                               "--timeout-seconds", "120", "--max-rss-mib", "2200", "--max-log-bytes", "2000000",
                               "--", sys.executable, str(ROOT / "tests/fixtures/probe_patched_multiion_native.py"),
                               str(project), str(output), "--route", route, "--engine", "GPU_DISK",
                               "--neutral-between-ions", *extra]
                    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=140)
                    row = {"name": name, "returnCode": result.returncode, "guard": str(guard),
                           "log": str(log), "output": str(output)}
                    report["cases"].append(row)
                    save()
                    if result.returncode != 0:
                        raise RuntimeError(f"{name} failed; see {log}")
                    output_data = json.loads(output.read_text(encoding="utf-8"))
                    module_path = Path(output_data["viennapsModulePath"]).resolve()
                    if not module_path.is_relative_to(runtime_dir):
                        raise RuntimeError(f"{name} used the wrong ViennaPS package: {module_path}")
                    if output_data["versions"] != {"viennaps": "4.6.2", "viennals": "5.8.5"}:
                        raise RuntimeError(f"{name} has unexpected engine versions")
                    if output_data["cudaDeviceReadback"]["uuid"] != args.device_uuid:
                        raise RuntimeError(f"{name} used an unexpected CUDA device")
                    outputs[variant] = output_data

                def mean_for(data, label):
                    index = data["speciesOrder"].index(label)
                    return statistics.fmean(data["particleFields"][f"{label}{index}"])

                baseline = outputs["baseline"]
                checks = {}
                for blocked, variant, other in (("xenonIon", "suppress-xenon", "argonIon"),
                                                ("argonIon", "suppress-argon", "xenonIon")):
                    original = mean_for(baseline, blocked)
                    suppressed = mean_for(outputs[variant], blocked)
                    other_before = mean_for(baseline, other)
                    other_after = mean_for(outputs[variant], other)
                    checks[blocked] = {"baselineMean": original, "suppressedMean": suppressed,
                                       "otherBaselineMean": other_before, "otherVariantMean": other_after,
                                       "passed": original > 0 and abs(suppressed) < original * 1e-6
                                       and other_before > 0 and abs(other_after - other_before) <= 0.1 * other_before}
                report["parameterChecks"].append({"dimension": dimension, "route": route, "ions": checks})
                save()
                if not all(row["passed"] for row in checks.values()):
                    raise RuntimeError(f"{dimension}D {route} ion parameters did not affect the correct field")
        if args.check_empty_map:
            project = EVIDENCE / "stock-single-neutral-project.json"
            neutral_outputs = {}
            for variant, extra in (("scalar", []), ("empty-map", ["--force-empty-sticking-map"])):
                name = f"candidate-neutral-{variant}"
                output, guard, log = (EVIDENCE / f"{name}.{suffix}" for suffix in ("json", "guard.json", "log"))
                command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                           "--record", str(guard), "--log-path", str(log), "--parent-pid", str(os.getpid()),
                           "--timeout-seconds", "120", "--max-rss-mib", "2200", "--max-log-bytes", "2000000",
                           "--", sys.executable, str(ROOT / "tests/fixtures/probe_patched_multiion_native.py"),
                           str(project), str(output), "--route", "gpu-specific", "--engine", "GPU_DISK", *extra]
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=140)
                report["cases"].append({"name": name, "returnCode": result.returncode, "guard": str(guard),
                                        "log": str(log), "output": str(output)})
                save()
                if result.returncode != 0:
                    raise RuntimeError(f"{name} failed; see {log}")
                data = json.loads(output.read_text(encoding="utf-8"))
                if not Path(data["viennapsModulePath"]).resolve().is_relative_to(runtime_dir):
                    raise RuntimeError(f"{name} used the wrong ViennaPS package")
                if data["versions"] != {"viennaps": "4.6.2", "viennals": "5.8.5"}:
                    raise RuntimeError(f"{name} has unexpected engine versions")
                if data["cudaDeviceReadback"]["uuid"] != args.device_uuid:
                    raise RuntimeError(f"{name} used an unexpected CUDA device")
                neutral_outputs[variant] = statistics.fmean(data["particleFields"]["fluorineNeutral0"])
            scalar, empty_map = neutral_outputs["scalar"], neutral_outputs["empty-map"]
            passed = scalar > 0 and abs(empty_map - scalar) <= 0.1 * scalar
            report["neutralOverloadCheck"] = {"scalarMean": scalar, "emptyMapMean": empty_map, "passed": passed}
            save()
            if not passed:
                raise RuntimeError("empty sticking map and scalar neutral flux differ")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    save()
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]),
                      "report": str(report_path), "error": report.get("error")}), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
