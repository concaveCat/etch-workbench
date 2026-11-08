"""Verify the installed GPU MultiParticle single-neutral support in the product worker."""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_stage7 import multi_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-neutral"
RUNS = ROOT / "var/gpu-coverage-audit-multiparticle-neutral"


def product_signature(snapshot: dict, dimension: int) -> dict:
    return {"points": snapshot["points"], "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def bounded(name: str, kind: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
        "--record", str(EVIDENCE / f"{name}.{kind}.guard.json"),
        "--log-path", str(EVIDENCE / f"{name}.{kind}.log"), "--timeout-seconds", "120",
        "--max-rss-mib", "1800", "--max-log-bytes", "400000", "--", *command],
        cwd=ROOT, capture_output=True, text=True, timeout=130, check=False)


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        for operation in ("etch", "deposit"):
            project = multi_project(dimension, operation)
            project["solver"]["enginePreference"] = "gpu"
            step = project["steps"][0]
            step["species"] = [{"kind": "neutral", "label": "neutral",
                                "defaultStickingProbability": 0.4,
                                "stickingProbabilityByMaterial": {"SiO2": 0.6}}]
            step["rateCoefficients"] = {"defaultBySpecies": {"neutral": 0.025},
                                         "byMaterial": {"SiO2": {"neutral": 0.04}}}
            name = f"multiParticle-{dimension}d-{operation}-single-neutral-gpu_disk"
            project_path = EVIDENCE / f"{name}.project.json"
            direct_path = EVIDENCE / f"{name}.direct.json"
            project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            run_dir = RUNS / name
            if run_dir.exists():
                shutil.rmtree(run_dir)
            run_dir.mkdir(parents=True, mode=0o700)
            input_path = run_dir / "input.json"
            input_path.write_text(json.dumps({"kind": "simulate", "runId": name, "project": project,
                "runSeed": project["solver"]["random"]["seed"],
                "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                     "viennals": importlib.metadata.version("viennals")},
                "stepEngines": {step["id"]: "GPU_DISK"}, "customMaterials": []}, indent=2) + "\n",
                encoding="utf-8")
            product = bounded(name, "product", [str(ROOT / ".venv/bin/python"),
                str(ROOT / "worker/simulate.py"), str(input_path), str(run_dir)])
            row = {"dimension": dimension, "operation": operation, "engine": "GPU_DISK",
                   "speciesKind": "neutral-only", "status": "failed", "productReturnCode": product.returncode}
            result_path = run_dir / "worker-result.json"
            if product.returncode == 0 and result_path.is_file():
                worker = json.loads(result_path.read_text(encoding="utf-8"))
                initial = json.loads((run_dir / worker["snapshots"][0]["snapshot"]).read_text(encoding="utf-8"))
                final = json.loads((run_dir / worker["snapshots"][-1]["snapshot"]).read_text(encoding="utf-8"))
                direct = bounded(name, "direct", [str(ROOT / ".venv/bin/python"),
                    str(ROOT / "tests/fixtures/stage7_multiparticle_reference.py"),
                    str(project_path), str(direct_path), "GPU_DISK"])
                if direct.returncode == 0 and direct_path.is_file():
                    reference = json.loads(direct_path.read_text(encoding="utf-8"))
                    native = reference["steps"][step["id"]]
                    actual = worker["stepRecords"][0]
                    comparisons = {
                        "initial": product_signature(initial, dimension) == direct_signature(reference["initial"]),
                        "final": product_signature(final, dimension) == direct_signature(native["snapshot"]),
                        "callbackCount": actual["actualParameters"]["rateCallbackSummary"]["invocationCount"] == native["callback"]["count"],
                        "callbackMaterials": actual["actualParameters"]["rateCallbackSummary"]["materials"] == native["callback"]["materials"],
                        "callbackSamples": actual["actualParameters"]["rateCallbackSummary"]["samples"] == native["callback"]["samples"],
                        "productEngine": actual["actualEngine"] == "GPU_DISK",
                        "directEngine": native["engine"] == "GPU_DISK",
                    }
                    product_evidence = EVIDENCE / f"{name}.product"
                    product_evidence.mkdir(exist_ok=True)
                    shutil.copy2(project_path, product_evidence / "project.json")
                    shutil.copy2(input_path, product_evidence / "worker-input.json")
                    shutil.copy2(result_path, product_evidence / "worker-result.json")
                    for snapshot_row in (worker["snapshots"][0], worker["snapshots"][-1]):
                        shutil.copy2(run_dir / snapshot_row["snapshot"], product_evidence / snapshot_row["snapshot"])
                        shutil.copy2(run_dir / snapshot_row["vtk"], product_evidence / snapshot_row["vtk"])
                    row.update({"status": "verified" if all(comparisons.values()) else "mismatch",
                        "comparisons": comparisons, "versions": worker["versions"],
                        "productWorker": str((product_evidence / "worker-result.json").relative_to(ROOT)),
                        "directReference": str(direct_path.relative_to(ROOT)),
                        "guards": [str((EVIDENCE / f"{name}.{kind}.guard.json").relative_to(ROOT))
                                   for kind in ("product", "direct")], "actualExecution": actual,
                        "speciesSignature": {"species": step["species"], "rateCoefficients": step["rateCoefficients"]}})
                else:
                    row.update({"status": "direct-reference-failed", "directReturnCode": direct.returncode,
                                "directStdout": direct.stdout[-2000:], "directStderr": direct.stderr[-4000:]})
            else:
                row.update({"productStdout": product.stdout[-2000:], "productStderr": product.stderr[-4000:]})
            (EVIDENCE / f"{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"dimension": dimension, "operation": operation, "status": row["status"],
                              "failure": row.get("productStderr", row.get("directStderr", ""))[-500:]}, ensure_ascii=False), flush=True)
            rows.append(row)
    summary = {"status": "passed" if all(row["status"] == "verified" for row in rows) else "incomplete",
               "passed": all(row["status"] == "verified" for row in rows),
               "versions": {"viennaps": importlib.metadata.version("viennaps"),
                            "viennals": importlib.metadata.version("viennals")}, "rows": rows}
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
