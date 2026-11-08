"""Bounded GPU probe for schema-7 custom-material rate tables in 2D/3D."""
from __future__ import annotations

import importlib.metadata
import json
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
from stage8_project_factory import make_project

EVIDENCE = ROOT / "docs/stage9-evidence/gpu-custom-material"
RUNS = ROOT / "var/stage9-gpu-custom-material"
CUSTOM = {"name": "GpuProbeFilm", "id": 177, "customId": 0, "color": "#7755aa",
          "conductive": False, "description": "Stage 9 bounded GPU capability probe"}


def signature(snapshot: dict, dimension: int) -> dict:
    return {"points": snapshot["points"], "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def direct_signature(snapshot: dict) -> dict:
    return {key: snapshot[key] for key in ("points", "cells", "materialIds", "materialIdsByLevelSetIndex")}


def product_signature(snapshot: dict, dimension: int) -> dict:
    return {"points": snapshot["points"],
            "cells": snapshot["lines"] if dimension == 2 else snapshot["triangles"],
            "materialIds": snapshot["cellData"]["materialIds"],
            "materialIdsByLevelSetIndex": snapshot["materialIdsByLevelSetIndex"]}


def browser_ui_acceptance() -> dict:
    report_path = EVIDENCE / "browser" / "acceptance.json"
    if not report_path.is_file():
        return {"status": "missing", "report": str(report_path.relative_to(ROOT)),
                "reason": "real browser acceptance report is missing", "cases": []}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        runner_path = ROOT / report["runnerGuard"] if report.get("runnerGuard") else (
            report_path.parent / "browser-runner-ui-fix.guard.json")
        runner_guard = json.loads(runner_path.read_text(encoding="utf-8"))
        expected_device = report["device"]["uuid"]
        checks = []
        for case in report["cases"]:
            dimension = int(case["dimension"])
            evidence_dir = ROOT / case["productEvidence"]
            required = ("project.json", "submitted-project.json", "exported-project.json", "worker-result.json",
                        "child-record.json", "initial.json", "final.json", "replay-worker-result.json",
                        "replay-child-record.json", "replay-final.json")
            if any(not (evidence_dir / name).is_file() for name in required):
                raise ValueError(f"{dimension}D browser product evidence is incomplete")
            product = json.loads((evidence_dir / "worker-result.json").read_text(encoding="utf-8"))
            replay_product = json.loads((evidence_dir / "replay-worker-result.json").read_text(encoding="utf-8"))
            project = json.loads((evidence_dir / "submitted-project.json").read_text(encoding="utf-8"))
            exported = json.loads((evidence_dir / "exported-project.json").read_text(encoding="utf-8"))
            direct = json.loads((ROOT / case["directReference"]).read_text(encoding="utf-8"))
            product_guard = json.loads((evidence_dir / "child-record.json").read_text(encoding="utf-8"))
            replay_guard = json.loads((evidence_dir / "replay-child-record.json").read_text(encoding="utf-8"))
            step_id = project["steps"][0]["id"]
            product_step = next(row for row in product["stepRecords"] if row["stepId"] == step_id)
            replay_step = next(row for row in replay_product["stepRecords"] if row["stepId"] == step_id)
            material = case["material"]
            saved_definition = next(row for row in project["materialDefinitions"] if row["name"] == material["name"])
            exported_definition = next(row for row in exported["materialDefinitions"] if row["name"] == material["name"])
            ui_checks = {
                "engine": case["engine"] == "GPU_DISK" and product_step["actualEngine"] == "GPU_DISK",
                "selectedDevice": case["gpuDeviceId"] == expected_device == product["cudaDeviceReadback"]["uuid"],
                "cudaDeviceVerifiedAfterApply": product_step["actualParameters"]["cudaDeviceReadback"]["afterProcessApply"]["uuid"] == expected_device
                    and product_step["actualParameters"]["cudaDeviceReadback"]["afterProcessApply"]["synchronizeStatus"] == 0,
                "processVisibilityRecord": product_step["actualParameters"]["cudaDeviceReadback"]["process"]["processListStatus"]
                    in {"verified", "not-observed", "unavailable"},
                "materialIdAndRate": saved_definition["id"] == material["id"] == exported_definition["id"]
                    and product_step["actualParameters"]["actualMaterialRatesById"][str(material["id"])] == -0.2,
                "replayDeviceAndEngine": replay_product["cudaDeviceReadback"]["uuid"] == expected_device
                    and replay_step["actualEngine"] == "GPU_DISK",
                "initialGeometry": product_signature(json.loads((evidence_dir / "initial.json").read_text()), dimension)
                    == direct_signature(direct["initial"]),
                "finalGeometry": product_signature(json.loads((evidence_dir / "final.json").read_text()), dimension)
                    == direct_signature(direct["final"]),
                "replayFinalGeometry": product_signature(json.loads((evidence_dir / "replay-final.json").read_text()), dimension)
                    == direct_signature(direct["final"]),
                "boundedProductAndReplay": product_guard["returnCode"] == 0
                    and replay_guard["returnCode"] == 0
                    and not product_guard["remainingOwnedGroupMembers"]
                    and not replay_guard["remainingOwnedGroupMembers"],
                "projectPersistence": case["savedReopenedExportedAndReplayedWithStableMaterialId"]
                    and project["solver"]["gpuDeviceId"] == expected_device
                    and exported["solver"]["gpuDeviceId"] == expected_device,
            }
            if not all(ui_checks.values()):
                raise ValueError(f"{dimension}D browser evidence checks failed: {ui_checks}")
            checks.append({"dimension": dimension, "materialId": material["id"], "gpuDeviceId": expected_device,
                           "checks": ui_checks, "productEvidence": case["productEvidence"],
                           "directReference": case["directReference"]})
        passed = (report.get("status") == "passed" and report.get("installedVersions") ==
                  {"viennaps": "4.6.2", "viennals": "5.8.5"}
                  and report.get("pageErrors") == [] and {row["dimension"] for row in checks} == {2, 3}
                  and runner_guard.get("returnCode") == 0
                  and not runner_guard.get("remainingOwnedGroupMembers"))
        return {"status": "passed" if passed else "incomplete", "report": str(report_path.relative_to(ROOT)),
                "runnerGuard": str(runner_path.relative_to(ROOT)), "device": report.get("device"), "cases": checks,
                "reason": None if passed else "browser report, installed versions, dimensions or outer guard did not pass"}
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        return {"status": "incomplete", "report": str(report_path.relative_to(ROOT)),
                "reason": f"user-facing evidence check failed: {exc}", "cases": []}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        project = make_project("sf6o2", dimension)
        project["name"] = f"Stage 9 GPU custom material {dimension}D"
        project["solver"]["enginePreference"] = "gpu"
        project["materialDefinitions"] = [CUSTOM]
        project["geometry"]["substrateMaterial"] = CUSTOM["name"]
        if dimension == 3:
            project["view3D"]["visibleMaterialIds"].append(CUSTOM["id"])
            project["view3D"]["opacityByMaterial"][str(CUSTOM["id"])] = 1.0
        step = project["steps"][0]
        for key in ("nativeModel", "nativeParameters"):
            step.pop(key, None)
        step.update({"model": "singleParticle", "operation": "etch", "rateMagnitude": 0.2,
                     "duration": 0.2, "stickingProbability": 0.5, "sourceExponent": 1.0,
                     "rateMode": "material", "materialRates": {CUSTOM["name"]: 0.2, "SiO2": 0.1, "Si3N4": 0.0}})
        run_id = secrets.token_hex(12)
        run_dir = RUNS / run_id
        run_dir.mkdir(mode=0o700)
        name = f"custom-material-{dimension}d-gpu_disk"
        project_path = EVIDENCE / f"{name}.project.json"
        product_path = EVIDENCE / f"{name}.product.json"
        direct_path = EVIDENCE / f"{name}.direct.json"
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        config_path = run_dir / "input.json"
        config_path.write_text(json.dumps({"kind": "simulate", "runId": run_id, "project": project,
            "runSeed": project["solver"]["random"]["seed"],
            "expectedVersions": {"viennaps": importlib.metadata.version("viennaps"),
                                 "viennals": importlib.metadata.version("viennals")},
            "stepEngines": {step["id"]: "GPU_DISK"}, "customMaterials": project["materialDefinitions"]}, indent=2) + "\n",
            encoding="utf-8")
        product_command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                           "--record", str(EVIDENCE / f"{name}.product.guard.json"),
                           "--log-path", str(EVIDENCE / f"{name}.product.log"), "--timeout-seconds", "100",
                           "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                           str(ROOT / ".venv/bin/python"), str(ROOT / "worker/simulate.py"), str(config_path), str(run_dir)]
        product_process = subprocess.run(product_command, cwd=ROOT, capture_output=True, text=True, timeout=110, check=False)
        row = {"dimension": dimension, "model": "singleParticle", "material": CUSTOM["name"],
               "engine": "GPU_DISK", "status": "failed", "returnCode": product_process.returncode}
        result_path = run_dir / "worker-result.json"
        if product_process.returncode == 0 and result_path.is_file():
            worker = json.loads(result_path.read_text(encoding="utf-8"))
            product_initial = json.loads((run_dir / worker["snapshots"][0]["snapshot"]).read_text(encoding="utf-8"))
            product_final = json.loads((run_dir / worker["snapshots"][-1]["snapshot"]).read_text(encoding="utf-8"))
            direct_command = [sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                              "--record", str(EVIDENCE / f"{name}.direct.guard.json"),
                              "--log-path", str(EVIDENCE / f"{name}.direct.log"), "--timeout-seconds", "100",
                              "--max-rss-mib", "1536", "--max-log-bytes", "300000", "--",
                              str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage9_custom_material_gpu_reference.py"),
                              str(project_path), str(direct_path)]
            direct_process = subprocess.run(direct_command, cwd=ROOT, capture_output=True, text=True, timeout=110, check=False)
            if direct_process.returncode == 0 and direct_path.is_file():
                direct = json.loads(direct_path.read_text(encoding="utf-8"))
                record = worker["stepRecords"][0]
                comparisons = {"initial": signature(product_initial, dimension) == direct_signature(direct["initial"]),
                               "final": signature(product_final, dimension) == direct_signature(direct["final"]),
                               "customNativeIdPresent": CUSTOM["id"] in product_initial["materialIdsByLevelSetIndex"] and
                                   CUSTOM["id"] in product_final["materialIdsByLevelSetIndex"],
                               "productEngine": record["actualEngine"] == "GPU_DISK",
                               "directEngine": direct["engine"] == "GPU_DISK"}
                row.update({"status": "succeeded" if all(comparisons.values()) else "mismatch", "runId": run_id,
                            "comparisons": comparisons, "versions": worker["versions"],
                            "product": str(result_path.relative_to(ROOT)), "directReference": str(direct_path.relative_to(ROOT)),
                            "guards": [str((EVIDENCE / f"{name}.{kind}.guard.json").relative_to(ROOT))
                                       for kind in ("product", "direct")], "actualExecution": record})
            else:
                row.update({"status": "direct-reference-failed", "directReturnCode": direct_process.returncode,
                            "directStdout": direct_process.stdout[-2000:], "directStderr": direct_process.stderr[-4000:]})
        else:
            row.update({"productStdout": product_process.stdout[-2000:], "productStderr": product_process.stderr[-4000:]})
        rows.append(row)
        (EVIDENCE / f"{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"dimension": dimension, "status": row["status"],
                          "failure": row.get("productStderr", row.get("directStderr", ""))[-500:]}, ensure_ascii=False), flush=True)
    browser = browser_ui_acceptance()
    passed = all(row["status"] == "succeeded" for row in rows) and browser["status"] == "passed"
    summary = {"status": "passed" if passed else "incomplete", "rows": rows,
               "passed": all(row["status"] == "succeeded" for row in rows),
               "exposedToWebUI": browser["status"] == "passed", "userFacingAcceptance": browser,
               "reason": browser.get("reason")}
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
