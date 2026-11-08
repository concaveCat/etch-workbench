"""Reader for v2 run records exposed to run inspection/replay clients."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from backend.v2_records import V2_RECORDS_VERSION, read_v2_steps

ROOT = Path(__file__).resolve().parents[1]


class V2RecordsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = ROOT / "var" / f"test-v2-records-{self._testMethodName}"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def test_reads_plan_outputs_and_state_without_arrays(self) -> None:
        step = self.work / "v2" / "implant_step_01" / "steps" / "implant_step_01"
        step.mkdir(parents=True)
        (step / "plan.json").write_text(json.dumps({"processId": "implant"}), encoding="utf-8")
        (step / "outputs.json").write_text(
            json.dumps({"schemaVersion": 1, "metrology": [{"metric": "sheetResistance", "value": 1.2}]}),
            encoding="utf-8",
        )
        (step / "state.json").write_text(
            json.dumps(
                {
                    "materials": {"order": [10], "names": {"10": "Si"}},
                    "geometry": {"dimension": 2, "gridDelta": 0.1},
                    "fieldsOmitted": True,
                    "fields": {"grid": {"shape": [2, 2]}},
                }
            ),
            encoding="utf-8",
        )
        records = read_v2_steps(self.work)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["stepId"], "implant_step_01")
        self.assertEqual(record["plan"]["processId"], "implant")
        self.assertEqual(record["outputs"]["metrology"][0]["value"], 1.2)
        self.assertTrue(record["state"]["fieldsOmitted"])
        self.assertTrue(record["state"]["hasFields"])
        self.assertFalse(record["state"]["fieldsPersisted"])
        self.assertNotIn("grid", record["state"])

    def test_missing_v2_directory_is_empty(self) -> None:
        self.assertEqual(read_v2_steps(self.work), [])
        self.assertEqual(V2_RECORDS_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
