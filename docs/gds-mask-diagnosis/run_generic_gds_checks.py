"""Run the native generic-GDS nesting probe for 2D/3D with both wrap modes."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "multi-layer.gds"


def main() -> int:
    output = HERE / "generic-acceptance"
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for dimension in (2, 3):
        for wrap in ("false", "true"):
            name = f"{dimension}d-wrap-{wrap}"
            report_path = output / f"{name}.json"
            guard_path = output / f"{name}.guard.json"
            command = [sys.executable, str(ROOT / "scripts/bounded_child.py"),
                       "--cwd", str(ROOT), "--record", str(guard_path),
                       "--log-path", str(output / f"{name}.log"),
                       "--timeout-seconds", "45", "--max-rss-mib", "2048", "--",
                       sys.executable, str(HERE / "generic_gds_probe.py"), str(dimension),
                       str(FIXTURE), str(report_path), "--wrap", wrap]
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=55)
            failures = []
            report = None
            if run.returncode != 0 or not report_path.exists():
                failures.append(f"native probe failed: {run.returncode}; {run.stderr[-300:]}")
            else:
                report = json.loads(report_path.read_text())
                after = report["after"]
                silicon = after["materials"].get("10", {}).get("bounds")
                if wrap == "false":
                    # The lower layer must be clipped to the top layer footprint;
                    # in 3D the whole Si level set collapses to zero points.
                    if silicon and max(abs(silicon[0]), abs(silicon[1])) > 1.0:
                        failures.append(f"wrap=false did not clip lower material: {silicon}")
                else:
                    if any(value == 0 for value in after["levelSetPoints"]):
                        failures.append("material level set vanished")
                    if not {"10", "30"} <= set(after["materials"]):
                        failures.append("Si/SiO2 surfaces missing")
                    if not silicon:
                        failures.append("Si material missing")
                    elif min(abs(silicon[0]), abs(silicon[1])) < 1.0:
                        failures.append(f"wrap=true lost lower material extent: {silicon}")
            row = {"name": name, "passed": not failures, "failures": failures,
                   "report": str(report_path.relative_to(ROOT))}
            rows.append(row)
            print(("PASS " if row["passed"] else "FAIL ") + name + (": " + str(failures) if failures else ""),
                  flush=True)
    summary = {"cases": len(rows), "passed": sum(row["passed"] for row in rows), "results": rows}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
