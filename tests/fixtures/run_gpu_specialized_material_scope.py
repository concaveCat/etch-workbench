"""Bounded native GPU/direct-reference sweep for selectable specialized-model substrate materials."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "tests/fixtures/run_gpu_specialized_geometry_coverage.py"
CATALOG = ROOT / "docs/materials-catalog.json"
OUT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/gpu-device-selection/material-scope"


def main() -> int:
    materials = [row["name"] for row in json.loads(CATALOG.read_text(encoding="utf-8"))["materials"]]
    rows = []
    for material in materials:
        material_dir = OUT / "selectable-substrates" / material
        material_dir.mkdir(parents=True, exist_ok=True)
        command = [str(PYTHON), str(RUNNER), "--geometry", "plane", "--dimension", "2",
                   "--substrate-material", material, "--engine", "GPU_DISK"]
        started_ns = time.time_ns()
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   timeout=900, check=False)
        targeted = ROOT / "docs/stage9-evidence/gpu-coverage-audit/specialized-geometries/targeted-summary.json"
        summary = (json.loads(targeted.read_text(encoding="utf-8"))
                   if targeted.is_file() and targeted.stat().st_mtime_ns >= started_ns else {"rows": []})
        saved_summary = material_dir / "summary.json"
        saved_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (material_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        (material_dir / "stderr.log").write_text(completed.stderr, encoding="utf-8")
        cases = summary.get("rows", [])
        row = {"material": material, "runnerReturnCode": completed.returncode,
               "summary": str(saved_summary.relative_to(ROOT)),
               "caseCount": len(cases),
               "verifiedCount": sum(case.get("status") == "verified" for case in cases),
               "cases": [{"name": case.get("name"), "status": case.get("status"),
                          "comparisons": case.get("comparisons"),
                          "productWorker": case.get("productWorker"),
                          "directReference": case.get("directReference"),
                          "productGuard": case.get("productGuard"), "directGuard": case.get("directGuard"),
                          "productFailure": case.get("productFailure"), "directFailure": case.get("directFailure")}
                         for case in cases]}
        rows.append(row)
        print(json.dumps({"material": material, "caseCount": row["caseCount"],
                          "verifiedCount": row["verifiedCount"],
                          "failed": [case["name"] for case in row["cases"] if case["status"] != "verified"]},
                         ensure_ascii=False), flush=True)
    models = sorted({case.get("model") for row in rows for case in json.loads(
        (ROOT / row["summary"]).read_text(encoding="utf-8")).get("rows", [])})
    complete = (len(rows) == 13 and len(models) == 10
                and all(row["caseCount"] == len(models) and row["verifiedCount"] == len(models)
                        and row["runnerReturnCode"] == 0 for row in rows))
    result = {"schemaVersion": 1, "status": "passed" if complete else "incomplete",
              "engine": "GPU_DISK", "engineVersion": "4.6.2", "dimension": 2,
              "geometry": "plane", "models": models, "substrateMaterials": materials,
              "caseCount": sum(row["caseCount"] for row in rows),
              "verifiedCount": sum(row["verifiedCount"] for row in rows), "rows": rows}
    output = OUT / "selectable-substrates-summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "materials": len(materials),
                      "models": len(models), "cases": result["caseCount"],
                      "verified": result["verifiedCount"], "summary": str(output.relative_to(ROOT))},
                     ensure_ascii=False), flush=True)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
