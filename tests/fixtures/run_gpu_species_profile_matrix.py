"""Debug MultiParticle GPU species-count combinations through bounded workers."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/fixtures/run_gpu_geometry_coverage.py"
PYTHON = ROOT / ".venv/bin/python"
OUT = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe"
PROFILES = (
    "one-ion", "one-ion-one-neutral", "one-ion-two-neutrals", "neutral-ion-neutral",
    "two-neutrals", "eight-neutrals", "one-ion-seven-neutrals-first", "one-ion-seven-neutrals-last",
    "two-ion-same-distribution", "two-ion-different-energy", "ion-neutral-ion", "eight-ions",
)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for profile in PROFILES:
        command = [str(PYTHON), str(RUNNER), "--geometry", "plane", "--dimension", "2", "--dimension", "3",
                   "--model", "multiParticle", "--operation", "etch", "--species-profile", profile]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=1800, check=False)
        summary_path = ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets/targeted-summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {"rows": []}
        saved_summary = OUT / f"{profile}.summary.json"
        saved_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        row = {
            "profile": profile,
            "runnerReturnCode": result.returncode,
            "summary": str(saved_summary.relative_to(ROOT)),
            "stdout": str((OUT / f"{profile}.stdout.log").relative_to(ROOT)),
            "stderr": str((OUT / f"{profile}.stderr.log").relative_to(ROOT)),
            "cases": summary.get("rows", []),
        }
        (OUT / f"{profile}.stdout.log").write_text(result.stdout)
        (OUT / f"{profile}.stderr.log").write_text(result.stderr)
        if not row["cases"]:
            row["error"] = "runner did not produce an acceptance row"
        rows.append(row)
        print(json.dumps({"profile": profile, "runnerReturnCode": result.returncode,
                          "status": row["cases"][0].get("status") if row["cases"] else "missing",
                          "productFailure": row["cases"][0].get("productFailure") if row["cases"] else None,
                          "directFailure": row["cases"][0].get("directFailure") if row["cases"] else None},
                         ensure_ascii=False), flush=True)
    result = {
        "schemaVersion": 1,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "engine": "GPU_DISK",
        "installedVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
        "scope": "2D plane MultiParticle species type/count diagnostic; every native call is bounded",
        "profiles": rows,
    }
    output = OUT / "summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": str(output.relative_to(ROOT)), "profiles": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
