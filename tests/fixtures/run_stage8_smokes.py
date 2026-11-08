"""Run each Stage 8 native smoke test in its own bounded child process."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODELS = (
    "sf6o2", "cf4o2", "hbro2", "sf6c4f8", "fluorocarbon", "ionBeam", "wetEtch",
    "teosDeposition", "teospecvd", "ald", "oxidation", "oxideRegrowth", "selectiveEpitaxy", "neutralTransport",
)


def main(dimension: int, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for name in MODELS:
        result_path = output / f"{name}-{dimension}d.json"
        command = [
            sys.executable, str(ROOT / "scripts/bounded_child.py"), "--cwd", str(ROOT),
            "--record", str(output / f"{name}-{dimension}d.guard.json"),
            "--log-path", str(output / f"{name}-{dimension}d.log"),
            "--timeout-seconds", "120", "--max-rss-mib", "1536", "--max-log-bytes", "250000", "--",
            str(ROOT / ".venv/bin/python"), str(ROOT / "tests/fixtures/stage8_model_smoke.py"),
            name, str(dimension), str(result_path),
        ]
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=135)
        if completed.returncode != 0 or not result_path.is_file():
            result = {"model": name, "dimension": dimension, "status": "child-failed",
                      "returnCode": completed.returncode, "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-4000:]}
        else:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        results.append(result)
        print(json.dumps({"model": name, "status": result.get("status"), "error": result.get("error")}, ensure_ascii=False), flush=True)
    report = {"dimension": dimension, "models": results,
              "passed": all(row.get("status") == "succeeded" for row in results)}
    (output / f"smoke-summary-{dimension}d.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: run_stage8_smokes.py DIMENSION OUTPUT_DIRECTORY")
    raise SystemExit(main(int(sys.argv[1]), Path(sys.argv[2])))
