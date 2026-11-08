#!/usr/bin/env python3
"""Recheck historical MultiParticle GPU profile failures against native route.

Each product worker and independent direct ViennaPS reference is itself run via
scripts/bounded_child.py by run_gpu_geometry_coverage.py.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/fixtures/run_gpu_geometry_coverage.py"
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-native-product/profile-recheck"

CASES = [
    (2, 0.25, 64, None),
    (2, 0.25, 512, None),
    (3, 0.25, 64, 0.6),
    (3, 0.25, 256, 0.6),
    (3, 0.20, 1024, 0.6),
    (3, 0.25, 1024, 0.6),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reuse", action="store_true",
                        help="assemble the summary from existing per-case acceptance files without rerunning ViennaPS")
    cli = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension, cfl, rays, mask_height in CASES:
        geometry_mask_tag = f"-mask-{mask_height:g}" if mask_height is not None else ""
        acceptance_name = (
            f"multiParticle-{dimension}d-trench-direct{geometry_mask_tag}-cfl{cfl:g}-rays{rays}"
            f"-species-two-ion-different-energy-etch-gpu_disk"
        )
        acceptance_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets" \
            / acceptance_name / "acceptance.json"
        command = [
            str(ROOT / ".venv/bin/python"), str(RUNNER),
            "--geometry", "trench", "--model", "multiParticle", "--operation", "etch",
            "--dimension", str(dimension), "--engine", "GPU_DISK",
            "--species-profile", "two-ion-different-energy",
            "--time-step-ratio", str(cfl), "--rays-per-point", str(rays),
        ]
        if mask_height is not None:
            command += ["--mask-height", str(mask_height)]
        env = os.environ.copy()
        env.setdefault("CUDA_VISIBLE_DEVICES", "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d")
        env.setdefault("VIENNAPS_PATCHED_RUNTIME_DIR", "/tmp/viennaps-normal-fix-runtime")
        case_name = f"{dimension}d-trench-cfl{cfl:g}-rays{rays}"
        started = time.time_ns()
        if cli.reuse:
            proc_returncode = 0 if acceptance_path.is_file() else 1
        else:
            proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                                  timeout=600, check=False)
            proc_returncode = proc.returncode
            (EVIDENCE / f"{case_name}.stdout.log").write_text(proc.stdout, encoding="utf-8")
            (EVIDENCE / f"{case_name}.stderr.log").write_text(proc.stderr, encoding="utf-8")
        try:
            acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            acceptance = None
        case_verified = bool(acceptance and acceptance.get("status") == "verified")
        callback = (acceptance or {}).get("productCallbacks") or {}
        callback_summary = {key: callback.get(key) for key in
                            ("invocationCount", "materials", "fluxSums", "fluxMaxima", "meanFluxBySpecies")}
        rows.append({
            "case": case_name, "startedNs": started, "returnCode": proc_returncode,
            "command": command, "status": "verified" if proc_returncode == 0 and case_verified else "failed",
            "acceptancePath": str(acceptance_path.relative_to(ROOT)),
            "comparisons": (acceptance or {}).get("comparisons", {}),
            "productActualEngine": (acceptance or {}).get("productActualEngine"),
            "directActualEngine": (acceptance or {}).get("directActualEngine"),
            "productCallbacks": callback_summary,
            "counts": (acceptance or {}).get("counts"),
            "stdout": f"{case_name}.stdout.log", "stderr": f"{case_name}.stderr.log",
        })
    summary = {
        "schemaVersion": 1,
        "description": "Historical CPU/GPU mismatch profiles rechecked through patched native multi-ion GPU route against direct ViennaPS GPU_DISK.",
        "runtimeDirectory": os.environ.get("VIENNAPS_PATCHED_RUNTIME_DIR", "/tmp/viennaps-normal-fix-runtime"),
        "cudaVisibleDevices": os.environ.get("CUDA_VISIBLE_DEVICES", "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d"),
        "cases": rows,
        "verified": sum(row["status"] == "verified" for row in rows),
        "failed": sum(row["status"] != "verified" for row in rows),
    }
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
    print(json.dumps({"verified": summary["verified"], "failed": summary["failed"],
                      "evidence": str((EVIDENCE / "summary.json").relative_to(ROOT)),
                      "cases": [{"case": row["case"], "status": row["status"]} for row in rows]},
                     ensure_ascii=False))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
