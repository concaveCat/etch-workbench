"""Real GPU_DISK runs above the former fixed 2D/3D grid and mesh limits."""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage8_project_factory import make_project

EVIDENCE = ROOT / "docs/stage9-evidence/high-resolution"
RUNS = ROOT / "var/high-resolution-acceptance"


def project_for(dimension: int) -> dict:
    project = make_project("sf6o2", dimension)
    project["name"] = f"High resolution GPU DISK {dimension}D acceptance"
    project["solver"].update(enginePreference="gpu", raysPerPoint=16, timeoutSeconds=600)
    if dimension == 2:
        project["domain"].update(gridDelta=0.005, xExtent=40.0)
    else:
        project["domain"].update(gridDelta=0.02, xExtent=6.0, yExtent=6.0)
    step = project["steps"][0]
    step.pop("nativeModel")
    step.pop("nativeParameters")
    step.update(model="singleParticle", rateMode="uniform", operation="etch", rateMagnitude=0.05,
                duration=0.02, stickingProbability=0.5, sourceExponent=1.0)
    return project


def run_bounded(name: str, kind: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
        "--record", str(EVIDENCE / f"{name}.{kind}.guard.json"),
        "--log-path", str(EVIDENCE / f"{name}.{kind}.log"), "--timeout-seconds", "600",
        "--max-rss-mib", "1900", "--max-log-bytes", "800000", "--", *command],
        cwd=ROOT, capture_output=True, text=True, timeout=610, check=False)


def signatures_match(product_path: Path, direct_path: Path, dimension: int) -> dict[str, bool]:
    product = json.loads(product_path.read_text(encoding="utf-8"))
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    cells_key = "lines" if dimension == 2 else "triangles"
    return {"points": product["points"] == direct["points"],
            "cells": product[cells_key] == direct["cells"],
            "materialIds": product["cellData"]["materialIds"] == direct["materialIds"],
            "materialIdsByLevelSetIndex": product["materialIdsByLevelSetIndex"] == direct["materialIdsByLevelSetIndex"]}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        project = project_for(dimension)
        name = f"{dimension}d-above-former-resolution-limits"
        project_path = EVIDENCE / f"{name}.project.json"
        direct_path = EVIDENCE / f"{name}.direct.json"
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run_dir = RUNS / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, mode=0o700)
        step = project["steps"][0]
        input_path = run_dir / "input.json"
        input_path.write_text(json.dumps({"kind": "simulate", "runId": name, "project": project,
            "runSeed": project["solver"]["random"]["seed"],
            "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                 "viennals": importlib.metadata.version("viennals")},
            "stepEngines": {step["id"]: "GPU_DISK"}, "customMaterials": []}, indent=2) + "\n",
            encoding="utf-8")
        product = run_bounded(name, "product", [str(ROOT / ".venv/bin/python"),
            str(ROOT / "worker/simulate.py"), str(input_path), str(run_dir)])
        row = {"dimension": dimension, "status": "failed", "productReturnCode": product.returncode,
               "gridDelta": project["domain"]["gridDelta"], "xExtent": project["domain"]["xExtent"],
               "yExtent": project["domain"].get("yExtent"), "enginePreference": "GPU_DISK"}
        result_path = run_dir / "worker-result.json"
        if product.returncode == 0 and result_path.is_file():
            worker = json.loads(result_path.read_text(encoding="utf-8"))
            initial_meta, final_meta = worker["snapshots"][0], worker["snapshots"][-1]
            direct = run_bounded(name, "direct", [str(ROOT / ".venv/bin/python"),
                str(ROOT / "tests/fixtures/gpu_singleparticle_direct_reference.py"),
                str(project_path), str(direct_path)])
            if direct.returncode == 0 and direct_path.is_file():
                direct = json.loads(direct_path.read_text(encoding="utf-8"))
                # Extract each reference half separately so the large product and direct meshes
                # are never retained together with both initial/final copies in memory.
                for label in ("initial", "final"):
                    ref_half = RUNS / f"{name}.direct-{label}.json"
                    ref_half.write_text(json.dumps(direct[label], ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                                        encoding="utf-8")
                compare = {
                    label: signatures_match(run_dir / meta["snapshot"], RUNS / f"{name}.direct-{label}.json", dimension)
                    for label, meta in (("initial", initial_meta), ("final", final_meta))}
                for label in ("initial", "final"):
                    (RUNS / f"{name}.direct-{label}.json").unlink(missing_ok=True)
                record = worker["stepRecords"][0]
                cells_key = "lineCount" if dimension == 2 else "triangleCount"
                rows_count = {"initialPoints": initial_meta["pointCount"], "finalPoints": final_meta["pointCount"],
                              "initialCells": initial_meta[cells_key], "finalCells": final_meta[cells_key]}
                product_evidence = EVIDENCE / f"{name}.product"
                product_evidence.mkdir(exist_ok=True)
                shutil.copy2(project_path, product_evidence / "project.json")
                shutil.copy2(input_path, product_evidence / "worker-input.json")
                shutil.copy2(result_path, product_evidence / "worker-result.json")
                for meta in (initial_meta, final_meta):
                    shutil.copy2(run_dir / meta["snapshot"], product_evidence / meta["snapshot"])
                    shutil.copy2(run_dir / meta["vtk"], product_evidence / meta["vtk"])
                guard_product = json.loads((EVIDENCE / f"{name}.product.guard.json").read_text())
                guard_direct = json.loads((EVIDENCE / f"{name}.direct.guard.json").read_text())
                row.update({"status": "verified" if all(all(values.values()) for values in compare.values()) and
                            record["actualEngine"] == "GPU_DISK" and direct["engine"] == "GPU_DISK" else "mismatch",
                    "comparisons": compare, "versions": worker["versions"], "counts": rows_count,
                    "actualEngine": record["actualEngine"], "actualSeed": record["actualSeed"],
                    "productPeakObservedRssBytes": guard_product["peakObservedRssBytes"],
                    "directPeakObservedRssBytes": guard_direct["peakObservedRssBytes"],
                    "productArtifacts": str(product_evidence.relative_to(ROOT)),
                    "directReference": str(direct_path.relative_to(ROOT))})
            else:
                row.update({"directReturnCode": direct.returncode, "directStdout": direct.stdout[-2000:],
                            "directStderr": direct.stderr[-4000:]})
        else:
            row.update({"productStdout": product.stdout[-2000:], "productStderr": product.stderr[-4000:]})
        (EVIDENCE / f"{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"dimension": dimension, "status": row["status"], "counts": row.get("counts"),
                          "failure": row.get("productStderr", row.get("directStderr", ""))[-500:]}, ensure_ascii=False), flush=True)
        rows.append(row)
    summary = {"status": "passed" if all(row["status"] == "verified" for row in rows) else "incomplete",
               "passed": all(row["status"] == "verified" for row in rows),
               "versions": {"viennaps": importlib.metadata.version("viennaps"), "viennals": importlib.metadata.version("viennals")},
               "rows": rows}
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
