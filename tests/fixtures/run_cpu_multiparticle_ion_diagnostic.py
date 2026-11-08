"""Check whether installed CPU_DISK supports ion layouts rejected by GPU_DISK."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
RUNNER = ROOT / "tests/fixtures/run_gpu_geometry_coverage.py"
OUTPUT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/cpu-ion-diagnostic"
PROFILES = ("two-ion-same-distribution", "two-ion-different-energy", "ion-neutral-ion", "eight-ions")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for profile in PROFILES:
        command = [str(PYTHON), str(RUNNER), "--geometry", "plane", "--dimension", "2", "--dimension", "3",
                   "--model", "multiParticle", "--operation", "etch", "--species-profile", profile,
                   "--engine", "CPU_DISK"]
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
        temporary = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/targeted-summary.json"
        cases = json.loads(temporary.read_text(encoding="utf-8")).get("rows", []) if temporary.is_file() else []
        row = {"profile": profile, "runnerReturnCode": process.returncode,
               "stdout": process.stdout, "stderr": process.stderr, "cases": cases}
        (OUTPUT / f"{profile}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        rows.append(row)
        print(json.dumps({"profile": profile, "runnerReturnCode": process.returncode,
                          "cases": [{"dimension": case.get("dimension"), "status": case.get("status"),
                                     "comparisons": case.get("comparisons")} for case in cases]},
                         ensure_ascii=False), flush=True)
    all_cases = [case for row in rows for case in row["cases"]]
    result = {"schemaVersion": 1, "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "engine": "CPU_DISK", "models": PROFILES, "dimensions": [2, 3],
              "caseCount": len(all_cases), "expectedCaseCount": len(PROFILES) * 2,
              "passed": len(all_cases) == len(PROFILES) * 2
              and all(row["runnerReturnCode"] == 0 for row in rows)
              and all(case.get("status") == "verified" and all(case.get("comparisons", {}).values())
                      for case in all_cases),
              "rows": [{"profile": row["profile"], **case} for row in rows for case in row["cases"]]}
    result["status"] = "passed" if result["passed"] else "incomplete"
    summary = OUTPUT / "summary.json"
    summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary.relative_to(ROOT)), "passed": result["passed"],
                      "caseCount": result["caseCount"], "expectedCaseCount": result["expectedCaseCount"]},
                     ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
