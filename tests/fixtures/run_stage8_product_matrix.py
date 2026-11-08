"""Run the product worker for all installed specialized models under bounded_child."""
from __future__ import annotations

import importlib.metadata
import json
import secrets
import subprocess
import sys
import argparse
from pathlib import Path

from stage8_project_factory import make_project


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs" / "stage8-evidence" / "product-worker"
RUNS = ROOT / "var" / "stage8-product-worker"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--dimension", action="append", type=int, dest="dimensions")
    parser.add_argument("--engine", choices=("CPU_DISK", "GPU_DISK", "GPU_TRIANGLE"), default="CPU_DISK")
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE)
    parser.add_argument("--runs-dir", type=Path, default=RUNS)
    args = parser.parse_args()
    evidence_dir = args.evidence_dir.resolve()
    runs_dir = args.runs_dir.resolve()
    if not evidence_dir.is_relative_to(ROOT) or not runs_dir.is_relative_to(ROOT):
        parser.error("evidence and run directories must stay inside the candidate project")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    all_models = ("sf6o2", "cf4o2", "hbro2", "sf6c4f8", "fluorocarbon", "ionBeam", "wetEtch",
                  "teosDeposition", "teospecvd", "ald", "oxidation", "oxideRegrowth", "selectiveEpitaxy", "neutralTransport")
    models = tuple(args.models or all_models)
    dimensions = tuple(args.dimensions or (2, 3))
    rows = []
    for dimension in dimensions:
        for model in models:
            run_id = secrets.token_hex(12)
            run_dir = runs_dir / run_id
            run_dir.mkdir(mode=0o700)
            project = make_project(model, dimension)
            project["solver"]["enginePreference"] = "gpu" if args.engine.startswith("GPU_") else "cpu"
            config_path = run_dir / "input.json"
            config_path.write_text(json.dumps({"kind": "simulate", "runId": run_id, "project": project,
                                               "runSeed": project["solver"]["random"]["seed"],
                                               "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                                                    "viennals": importlib.metadata.version("viennals")},
                                               "stepEngines": {project["steps"][0]["id"]: args.engine},
                                               "customMaterials": []}, indent=2) + "\n", encoding="utf-8")
            name = f"{model}-{dimension}d" + ("" if args.engine == "CPU_DISK" else f"-{args.engine.lower()}")
            command = [sys.executable, str(ROOT / "scripts" / "bounded_child.py"), "--cwd", str(ROOT),
                       "--record", str(evidence_dir / f"{name}.final.guard.json"), "--log-path", str(evidence_dir / f"{name}.final.log"),
                       "--timeout-seconds", "100", "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                       str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "worker" / "simulate.py"),
                       str(config_path), str(run_dir)]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=110)
            worker_path = run_dir / "worker-result.json"
            if completed.returncode != 0 or not worker_path.is_file():
                row = {"model": model, "dimension": dimension, "status": "failed", "returnCode": completed.returncode,
                       "stdoutTail": completed.stdout[-3000:], "stderrTail": completed.stderr[-5000:]}
            else:
                worker = json.loads(worker_path.read_text(encoding="utf-8"))
                initial_meta, final_meta = worker["snapshots"][0], worker["snapshots"][-1]
                initial_snapshot = json.loads((run_dir / initial_meta["snapshot"]).read_text(encoding="utf-8"))
                final_snapshot = json.loads((run_dir / final_meta["snapshot"]).read_text(encoding="utf-8"))
                row = {"model": model, "dimension": dimension, "status": "succeeded",
                       "runId": run_id, "actualExecution": worker["stepRecords"],
                       "initial": initial_meta, "final": final_meta,
                       "versions": worker["versions"], "artifactDirectory": str(run_dir.relative_to(ROOT))}
                row["engine"] = args.engine
                (evidence_dir / f"{name}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                input_path = evidence_dir / f"{name}.final.input.json"
                reference_path = evidence_dir / f"{name}.final.direct-reference.json"
                scratch = runs_dir / f"direct-{run_id}"
                input_path.write_text(json.dumps(project, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                reference_command = [sys.executable, str(ROOT / "scripts" / "bounded_child.py"), "--cwd", str(ROOT),
                                     "--record", str(evidence_dir / f"{name}.final.direct.guard.json"),
                                     "--log-path", str(evidence_dir / f"{name}.final.direct.log"),
                                     "--timeout-seconds", "100", "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                                     str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "tests" / "fixtures" / "stage8_direct_reference.py"),
                                     str(input_path), str(reference_path), str(scratch), args.engine]
                reference_run = subprocess.run(reference_command, cwd=ROOT, capture_output=True, text=True, timeout=110)
                if reference_run.returncode != 0 or not reference_path.is_file():
                    row.update({"status": "reference-failed", "referenceReturnCode": reference_run.returncode,
                                "referenceStdoutTail": reference_run.stdout[-2000:], "referenceStderrTail": reference_run.stderr[-4000:]})
                else:
                    reference = json.loads(reference_path.read_text(encoding="utf-8"))
                    compare = {}
                    for label, product_snapshot, direct_snapshot in (("initial", initial_snapshot, reference["initial"]),
                                                                      ("final", final_snapshot, reference["final"])):
                        native_cells = product_snapshot.get("lines") if dimension == 2 else product_snapshot.get("triangles")
                        direct_cells = direct_snapshot["cells"]
                        product_arrays = {"points": product_snapshot["points"], "cells": native_cells,
                                          "materialIds": product_snapshot["cellData"]["materialIds"],
                                          "materialIdsByLevelSetIndex": product_snapshot["materialIdsByLevelSetIndex"]}
                        direct_arrays = {"points": direct_snapshot["points"], "cells": direct_cells,
                                         "materialIds": direct_snapshot["materialIds"],
                                         "materialIdsByLevelSetIndex": direct_snapshot["materialIdsByLevelSetIndex"]}
                        compare[label] = {key: product_arrays[key] == direct_arrays[key] for key in product_arrays}
                    if model == "oxideRegrowth":
                        cellset_artifact = next((row for row in final_snapshot.get("outputs", []) if row.get("kind") == "nativeCellSet"), None)
                        if cellset_artifact is None:
                            row["status"] = "missing-native-cellset-output"
                            compare["nativeCellSet"] = {"present": False}
                        else:
                            product_cellset = json.loads((run_dir / cellset_artifact["fileName"]).read_text(encoding="utf-8"))
                            direct_cellset = reference["final"].get("cellSet")
                            compare["nativeCellSet"] = {
                                "numberOfCells": product_cellset.get("numberOfCells") == (direct_cellset or {}).get("numberOfCells"),
                                "fields": product_cellset.get("fields") == (direct_cellset or {}).get("fields"),
                            }
                    row["directReference"] = {"file": str(reference_path.relative_to(ROOT)), "comparisons": compare}
                    row["status"] = "succeeded" if all(all(result.values()) for result in compare.values()) else "mismatch"
                (evidence_dir / f"{name}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            rows.append(row)
            print(json.dumps({"model": model, "dimension": dimension, "status": row["status"],
                              "failure": row.get("stderrTail", "")[-500:]}, ensure_ascii=False), flush=True)
    summary = {"schemaVersion": 1, "viennaps": importlib.metadata.version("viennaps"),
               "viennals": importlib.metadata.version("viennals"), "rows": rows,
               "passed": len(rows) == len(models) * len(dimensions) and all(row["status"] == "succeeded" for row in rows)}
    if args.engine == "CPU_DISK":
        summary_name = "summary.json" if models == all_models and dimensions == (2, 3) else "targeted-summary.json"
    else:
        summary_name = f"summary-{args.engine.lower()}.json"
    summary_path = evidence_dir / summary_name
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
