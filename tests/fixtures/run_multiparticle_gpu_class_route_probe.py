"""Compare the installed generic and GPU-specific MultiParticle class routes."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
EVIDENCE = ROOT / "docs/stage9-evidence/gpu-coverage-audit/multiparticle-species-probe/gpu-class-route"
PROBE = ROOT / "tests/fixtures/probe_multiparticle_gpu_class_steps.py"


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        name = f"gpu-specific-{dimension}d-two-ion-flux"
        project = (ROOT / "docs/stage9-evidence/gpu-coverage-audit/geometry-presets"
                   / f"multiParticle-{dimension}d-plane-direct-species-two-ion-different-energy-etch-gpu_disk/project.json")
        output = EVIDENCE / f"{name}.json"
        record = EVIDENCE / f"{name}.guard.json"
        log = EVIDENCE / f"{name}.log"
        command = [str(PYTHON), str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
                   "--record", str(record), "--log-path", str(log), "--timeout-seconds", "90",
                   "--max-rss-mib", "1800", "--max-log-bytes", "1000000",
                   "--terminate-grace-seconds", "2", "--", str(PYTHON), str(PROBE),
                   str(project), str(output), "initialize-context"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=100, check=False)
        result = json.loads(output.read_text(encoding="utf-8")) if output.is_file() else {}
        field_names = list(result.get("cellData", {}))
        ion_fields = [name for name in field_names if "ion" in name.lower()]
        ion_labels = [label for label in result.get("speciesOrder", []) if "ion" in label.lower()]
        observed_labels = [label for label in ion_labels
                           if any(label.lower() in name.lower() for name in ion_fields)]
        log_path = EVIDENCE / f"{name}.log"
        stages = []
        if log_path.is_file():
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    stages.append(json.loads(line).get("stage"))
                except ValueError:
                    continue
        row = {"dimension": dimension, "modelClass": result.get("modelClass"),
               "engine": result.get("engine"),
               "speciesOrder": result.get("speciesOrder"), "ionSpeciesExpected": ion_labels,
               "ionSpeciesObserved": observed_labels, "ionFieldsObserved": ion_fields,
               "ionFieldsComplete": bool(ion_labels) and set(observed_labels) == set(ion_labels),
               "completedStages": stages,
               "runnerReturnCode": completed.returncode, "output": str(output.relative_to(ROOT)),
               "guard": str(record.relative_to(ROOT)), "log": str(log_path.relative_to(ROOT)),
               "stdout": completed.stdout, "stderr": completed.stderr}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    completed = all(row["runnerReturnCode"] == 0 and row["engine"] == "GPU_DISK"
                    and "gpu-context-created" in row["completedStages"]
                    and row["completedStages"][-1:] == ["result-written"] for row in rows)
    summary = {"status": "completed" if completed else "incomplete", "engine": "GPU_DISK",
               "engineVersions": {"viennaps": "4.6.2", "viennals": "5.8.5"},
               "deviceUuid": "GPU-5532bb0f-735e-bc93-98ca-b7662a8ebf6d",
               "scope": "ViennaPS GPU-specific MultiParticleProcess class with explicit GPU Context and single-pass flux",
               "multiIonGpuSupport": "not-supported-by-installed-4.6.2-binding",
               "caseCount": len(rows), "expectedCaseCount": 2, "rows": rows}
    path = EVIDENCE / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "caseCount": len(rows),
                      "evidence": str(path.relative_to(ROOT))}, ensure_ascii=False))
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
