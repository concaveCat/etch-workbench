"""Validate the candidate C++ GPU model in bounded subprocesses."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-fix-2026-10-28"
sys.path.insert(0, str(ROOT))
from backend.gpu_devices import cuda_devices, resolve_gpu_device
from worker.simulate import cuda_runtime_readback


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-uuid", required=True)
    args = parser.parse_args()
    if os.environ.get("CUDA_VISIBLE_DEVICES") != args.device_uuid:
        raise RuntimeError("CUDA_VISIBLE_DEVICES must be pinned to the requested UUID")
    device = resolve_gpu_device(args.device_uuid, cuda_devices(refresh=True))
    report_path = EVIDENCE / "standalone-native-acceptance.json"
    report = {"status": "running", "device": device, "cudaReadback": cuda_runtime_readback(device),
              "viennapsSourceCommit": "4c9ee7d2e04e31e71f99180657ac141181c9e205",
              "viennarayCommit": "6cd664ceddebfa2ef67bda944e68886d880f889e", "cases": [], "checks": []}

    def save():
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    executable = EVIDENCE / "standalone-multiion-probe-era"
    ptx_dir = EVIDENCE / "standalone-ptx-era"
    try:
        for dimension in (2, 3):
            for route in ("generic", "gpu-specific"):
                outputs = {}
                variants = ["baseline", "suppress-argon", "suppress-xenon"]
                if route == "gpu-specific":
                    variants.append("empty-map")
                for variant in variants:
                    name = f"cpp-{dimension}d-{route}-{variant}"
                    output, guard, log = (EVIDENCE / f"{name}.{suffix}" for suffix in ("json", "guard.json", "log"))
                    command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                               "--record", str(guard), "--log-path", str(log), "--parent-pid", str(os.getpid()),
                               "--timeout-seconds", "120", "--max-rss-mib", "2200", "--max-log-bytes", "2000000",
                               "--", str(executable), str(dimension), route, variant, str(ptx_dir), str(output)]
                    result = subprocess.run(command, cwd=ROOT, env=os.environ.copy(), capture_output=True,
                                            text=True, timeout=140)
                    report["cases"].append({"name": name, "returnCode": result.returncode,
                                            "guard": str(guard), "log": str(log), "output": str(output)})
                    save()
                    if result.returncode:
                        raise RuntimeError(f"{name} failed; see {log}")
                    data = json.loads(output.read_text(encoding="utf-8"))
                    if data["status"] != "succeeded" or data["engine"] != "GPU_DISK" or data["callbacks"] <= 0:
                        raise RuntimeError(f"{name} did not complete the native GPU process")
                    outputs[variant] = data["fields"]
                base = outputs["baseline"]
                checks = {}
                for blocked, variant, other in (("argonIon0", "suppress-argon", "xenonIon2"),
                                                ("xenonIon2", "suppress-xenon", "argonIon0")):
                    a, b = base[blocked], outputs[variant][blocked]
                    c, d = base[other], outputs[variant][other]
                    checks[blocked] = {"baseline": a, "suppressed": b, "otherBaseline": c, "otherAfter": d,
                                       "passed": a > 0 and abs(b) < 1e-6 * a and c > 0 and abs(d-c) <= .1*c}
                if route == "gpu-specific":
                    x, y = base["fluorineNeutral1"], outputs["empty-map"]["fluorineNeutral1"]
                    checks["emptyNeutralMap"] = {"scalar": x, "emptyMap": y,
                                                 "passed": x > 0 and abs(x-y) <= .1*x}
                report["checks"].append({"dimension": dimension, "route": route, "results": checks})
                save()
                if not all(result["passed"] for result in checks.values()):
                    raise RuntimeError(f"{dimension}D {route}: parameter independence or neutral overload failed")
        report["cudaReadbackAfter"] = cuda_runtime_readback(device)
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
