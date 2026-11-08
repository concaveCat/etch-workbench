"""Run schema-7 MultiParticleProcess GPU_DISK product/direct-reference cases."""
from __future__ import annotations

import importlib.metadata
import json
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_stage7 import multi_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-multiparticle"
RUNS = ROOT / "var/stage9-gpu-multiparticle"


def mesh_signature(snapshot: dict, dimension: int) -> dict:
    return {"points": snapshot["points"], "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_mesh_signature(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        for operation in ("etch", "deposit"):
            project = multi_project(dimension, operation)
            project["solver"]["enginePreference"] = "gpu"
            run_id = secrets.token_hex(12)
            run_dir = RUNS / run_id
            run_dir.mkdir(mode=0o700)
            name = f"multiParticle-{dimension}d-{operation}-gpu_disk"
            project_path = EVIDENCE / f"{name}.project.json"
            product_output = EVIDENCE / f"{name}.product.json"
            direct_output = EVIDENCE / f"{name}.direct.json"
            project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            input_path = run_dir / "input.json"
            input_path.write_text(json.dumps({"kind": "simulate", "runId": run_id, "project": project,
                "runSeed": project["solver"]["random"]["seed"],
                "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                     "viennals": importlib.metadata.version("viennals")},
                "stepEngines": {project["steps"][0]["id"]: "GPU_DISK"}, "customMaterials": []}, indent=2) + "\n",
                encoding="utf-8")
            command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                       "--record", str(EVIDENCE / f"{name}.product.guard.json"),
                       "--log-path", str(EVIDENCE / f"{name}.product.log"), "--timeout-seconds", "100",
                       "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                       str(ROOT / ".venv/bin/python"), str(ROOT / "worker/simulate.py"), str(input_path), str(run_dir)]
            product = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=110, check=False)
            result_path = run_dir / "worker-result.json"
            row = {"dimension": dimension, "operation": operation, "model": "multiParticle", "engine": "GPU_DISK",
                   "runId": run_id, "inputProject": str(project_path.relative_to(ROOT)),
                   "status": "failed", "returnCode": product.returncode}
            if product.returncode == 0 and result_path.is_file():
                worker = json.loads(result_path.read_text(encoding="utf-8"))
                snapshots = []
                for meta in worker["snapshots"]:
                    snapshots.append(json.loads((run_dir / meta["snapshot"]).read_text(encoding="utf-8")))
                direct_command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                                  "--record", str(EVIDENCE / f"{name}.direct.guard.json"),
                                  "--log-path", str(EVIDENCE / f"{name}.direct.log"), "--timeout-seconds", "100",
                                  "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                                  str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage7_multiparticle_reference.py"),
                                  str(project_path), str(direct_output), "GPU_DISK"]
                direct = subprocess.run(direct_command, cwd=ROOT, capture_output=True, text=True, timeout=110, check=False)
                if direct.returncode == 0 and direct_output.is_file():
                    reference = json.loads(direct_output.read_text(encoding="utf-8"))
                    direct_step = reference["steps"][project["steps"][0]["id"]]
                    direct_initial = reference["initial"]
                    direct_final = direct_step["snapshot"]
                    product_record = next(record for record in worker["stepRecords"]
                                          if record.get("stepId") == project["steps"][0]["id"])
                    callback = product_record["actualParameters"]["rateCallbackSummary"]
                    comparisons = {
                        "initial": mesh_signature(snapshots[0], dimension) == direct_mesh_signature(direct_initial),
                        "final": mesh_signature(snapshots[-1], dimension) == direct_mesh_signature(direct_final),
                        "callbackCount": callback["invocationCount"] == direct_step["callback"]["count"],
                        "callbackMaterials": callback["materials"] == direct_step["callback"]["materials"],
                        "callbackSamples": callback["samples"] == direct_step["callback"]["samples"],
                        "productEngine": product_record["actualEngine"] == "GPU_DISK",
                        "directEngine": direct_step["engine"] == "GPU_DISK",
                    }
                    row.update({"status": "succeeded" if all(comparisons.values()) else "mismatch", "comparisons": comparisons,
                                "versions": worker["versions"], "product": str(result_path.relative_to(ROOT)),
                                "directReference": str(direct_output.relative_to(ROOT)),
                                "guards": [str((EVIDENCE / f"{name}.{kind}.guard.json").relative_to(ROOT))
                                           for kind in ("product", "direct")], "actualExecution": product_record})
                else:
                    row.update({"status": "direct-reference-failed", "directReturnCode": direct.returncode,
                                "directStdout": direct.stdout[-2000:], "directStderr": direct.stderr[-4000:]})
            else:
                row.update({"productStdout": product.stdout[-2000:], "productStderr": product.stderr[-4000:]})
            rows.append(row)
            (EVIDENCE / f"{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"dimension": dimension, "operation": operation, "status": row["status"],
                              "failure": row.get("productStderr", row.get("directStderr", ""))[-500:]}, ensure_ascii=False), flush=True)
    summary = {"schemaVersion": 1, "status": "passed" if all(row["status"] == "succeeded" for row in rows) else "incomplete",
               "rows": rows, "passed": all(row["status"] == "succeeded" for row in rows), "noFallback": True}
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
