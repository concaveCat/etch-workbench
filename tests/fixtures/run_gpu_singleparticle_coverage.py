"""Bounded product-worker vs independent direct GPU_DISK comparison."""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage8_project_factory import make_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/singleparticle"
RUNS = ROOT / "var/gpu-coverage-audit-singleparticle"


def product_signature(snapshot: dict, dimension: int) -> dict:
    return {"points": snapshot["points"], "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def run_bounded(name: str, kind: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
        "--record", str(EVIDENCE / f"{name}.{kind}.guard.json"),
        "--log-path", str(EVIDENCE / f"{name}.{kind}.log"), "--timeout-seconds", "120",
        "--max-rss-mib", "1800", "--max-log-bytes", "400000", "--", *command],
        cwd=ROOT, capture_output=True, text=True, timeout=130, check=False)


def make_generic_project(dimension: int, operation: str, pattern: str) -> dict:
    project = make_project("sf6o2", dimension)
    project["name"] = f"GPU_DISK generic singleParticle {dimension}D {operation} {pattern}"
    project["solver"]["enginePreference"] = "gpu"
    project["domain"]["gridDelta"] = 0.2
    project["domain"]["xExtent"] = 4.0
    if dimension == 3:
        project["domain"]["yExtent"] = 4.0
    project["geometry"]["pattern"] = ({"kind": "none"} if pattern == "none" else
                                         {"kind": "trench", "width": 1.2, "taperAngle": 0.0})
    step = project["steps"][0]
    step.pop("nativeModel")
    step.pop("nativeParameters")
    step.update({"model": "singleParticle", "operation": operation, "rateMagnitude": 0.2,
                 "duration": 0.2, "stickingProbability": 0.5, "sourceExponent": 1.0,
                 "rateMode": "uniform"})
    step.pop("depositMaterial", None)
    if operation == "deposit":
        step["depositMaterial"] = "SiO2"
    return project


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        for pattern in ("none", "trench"):
            for operation in ("etch", "deposit"):
                project = make_generic_project(dimension, operation, pattern)
                name = f"singleParticle-{dimension}d-{operation}-{pattern}-gpu_disk"
                project_path = EVIDENCE / f"{name}.project.json"
                project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                run_dir = RUNS / name
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                run_dir.mkdir(parents=True, mode=0o700)
                run_id = name
                config_path = run_dir / "input.json"
                config = {"kind": "simulate", "runId": run_id, "project": project,
                    "runSeed": project["solver"]["random"]["seed"],
                    "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                          "viennals": importlib.metadata.version("viennals")},
                    "stepEngines": {project["steps"][0]["id"]: "GPU_DISK"}, "customMaterials": []}
                config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                product = run_bounded(name, "product", [str(ROOT / ".venv/bin/python"),
                    str(ROOT / "worker/simulate.py"), str(config_path), str(run_dir)])
                row = {"dimension": dimension, "operation": operation, "pattern": pattern,
                       "engine": "GPU_DISK", "status": "failed", "productReturnCode": product.returncode}
                result_path = run_dir / "worker-result.json"
                if product.returncode == 0 and result_path.is_file():
                    worker = json.loads(result_path.read_text(encoding="utf-8"))
                    initial = json.loads((run_dir / worker["snapshots"][0]["snapshot"]).read_text(encoding="utf-8"))
                    final = json.loads((run_dir / worker["snapshots"][-1]["snapshot"]).read_text(encoding="utf-8"))
                    direct_path = EVIDENCE / f"{name}.direct.json"
                    direct = run_bounded(name, "direct", [str(ROOT / ".venv/bin/python"),
                        str(ROOT / "tests/fixtures/gpu_singleparticle_direct_reference.py"),
                        str(project_path), str(direct_path)])
                    if direct.returncode == 0 and direct_path.is_file():
                        reference = json.loads(direct_path.read_text(encoding="utf-8"))
                        actual_step = worker["stepRecords"][0]
                        comparisons = {
                            "initial": product_signature(initial, dimension) == direct_signature(reference["initial"]),
                            "final": product_signature(final, dimension) == direct_signature(reference["final"]),
                            "productEngine": actual_step["actualEngine"] == "GPU_DISK",
                            "directEngine": reference["engine"] == "GPU_DISK",
                            "seed": actual_step["actualSeed"] == reference["seed"],
                            "materials": sorted(initial["materialIdsByLevelSetIndex"]) == sorted(reference["initial"]["materialIdsByLevelSetIndex"]),
                        }
                        evidence_run = EVIDENCE / f"{name}.product"
                        evidence_run.mkdir(exist_ok=True)
                        shutil.copy2(project_path, evidence_run / "project.json")
                        shutil.copy2(config_path, evidence_run / "worker-input.json")
                        shutil.copy2(result_path, evidence_run / "worker-result.json")
                        for snapshot_row in (worker["snapshots"][0], worker["snapshots"][-1]):
                            shutil.copy2(run_dir / snapshot_row["snapshot"], evidence_run / snapshot_row["snapshot"])
                        for snapshot_row in (worker["snapshots"][0], worker["snapshots"][-1]):
                            shutil.copy2(run_dir / snapshot_row["vtk"], evidence_run / snapshot_row["vtk"])
                        row.update({"status": "verified" if all(comparisons.values()) else "mismatch",
                            "comparisons": comparisons, "versions": worker["versions"],
                            "productEvidence": str(evidence_run.relative_to(ROOT)),
                            "directReference": str(direct_path.relative_to(ROOT)),
                            "guards": [str((EVIDENCE / f"{name}.{kind}.guard.json").relative_to(ROOT))
                                       for kind in ("product", "direct")],
                            "actualExecution": actual_step,
                            "counts": {"initialPoints": len(initial["points"]),
                                       "finalPoints": len(final["points"]),
                                       "initialCells": len(initial["lines"] if dimension == 2 else initial["triangles"]),
                                       "finalCells": len(final["lines"] if dimension == 2 else final["triangles"])}})
                    else:
                        row.update({"status": "direct-reference-failed", "directReturnCode": direct.returncode,
                                    "directStdout": direct.stdout[-2000:], "directStderr": direct.stderr[-4000:]})
                else:
                    row.update({"productStdout": product.stdout[-2000:], "productStderr": product.stderr[-4000:]})
                (EVIDENCE / f"{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"dimension": dimension, "operation": operation, "pattern": pattern,
                                  "status": row["status"],
                                  "failure": row.get("productStderr", row.get("directStderr", ""))[-500:]},
                                 ensure_ascii=False), flush=True)
                rows.append(row)
    summary = {"status": "passed" if all(row["status"] == "verified" for row in rows) else "incomplete",
               "passed": all(row["status"] == "verified" for row in rows),
               "versions": {"viennaps": importlib.metadata.version("viennaps"),
                            "viennals": importlib.metadata.version("viennals")},
               "rows": rows}
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
