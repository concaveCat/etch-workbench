"""Run the complete candidate regression suite with isolated workbench data."""
from __future__ import annotations

import json
import os
import secrets
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_STAGE = os.environ.get("WORKBENCH_ACCEPTANCE_STAGE", "stage-current")
if not ACCEPTANCE_STAGE.replace("-", "").replace("_", "").isalnum():
    raise ValueError("WORKBENCH_ACCEPTANCE_STAGE must be a simple label")
EVIDENCE = ROOT / "docs" / f"{ACCEPTANCE_STAGE}-evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)

# Never run acceptance against either the formal workbench or the candidate
# service's normal var directory. The generated data remains for inspection.
data_dir = ROOT / "var" / f"acceptance-{secrets.token_hex(6)}"
os.environ["VIENNAPS_WORKBENCH_DATA_DIR"] = str(data_dir)
os.environ.setdefault("WORKBENCH_PORT", "8767")

sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))

class TeeStream:
    """Keep the full unittest transcript live on stdout and in the evidence directory."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def writeln(self, value: str = "") -> None:
        self.write(value + "\n")

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


transcript = EVIDENCE / "full-python-regression.log"
with transcript.open("w", encoding="utf-8") as log:
    result = unittest.TextTestRunner(stream=TeeStream(sys.stdout, log), verbosity=2).run(suite)
report = {
    "status": "passed" if result.wasSuccessful() else "failed",
    "testsRun": result.testsRun,
    "failures": [{"test": str(test), "traceback": text} for test, text in result.failures],
    "errors": [{"test": str(test), "traceback": text} for test, text in result.errors],
    "skipped": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
    "isolatedDataDirectory": str(data_dir),
    "transcript": str(transcript.relative_to(ROOT)),
    "gpuTestEnabled": os.environ.get("WORKBENCH_GPU_TEST") == "1",
}
(EVIDENCE / "full-python-regression.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Acceptance data: {data_dir}")
print(f"Acceptance report: {EVIDENCE / 'full-python-regression.json'}")
raise SystemExit(0 if result.wasSuccessful() else 1)
