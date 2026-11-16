"""Engine-layer contract tests (no SPX, no ViennaPS, no licence).

These tests cover the parts that must never regress mechanically:

* the job/step protocol rejects malformed input instead of guessing;
* the scheduler aligns by ``step_id``, never interpolates and never substitutes
  one engine for another;
* ``compare.json`` is deterministic for the same job.

Real engine runs are covered by the opt-in integration test at the bottom, which
only runs when ``VIENNAPS_WORKBENCH_ENGINE_INTEGRATION=1`` is set.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engines import protocol, scheduler  # noqa: E402
from engines.registry import EngineRegistration  # noqa: E402

FAKE_ENGINE = textwrap.dedent('''
    import json, sys, pathlib
    job = json.loads(pathlib.Path(sys.argv[1]).read_text())
    out = pathlib.Path(sys.argv[2]); (out / "frames").mkdir(parents=True, exist_ok=True)
    rows = []
    for step in job["steps"]:
        if step["step_id"] == "s_missing":
            continue
        if step["step_id"] == "s_unsupported":
            rows.append({"step_id": step["step_id"], "status": "unsupported",
                         "wall_ms": 1.0, "frame": None, "checkpoint": None,
                         "metrics": {}, "reason": "fake engine refuses"})
            continue
        frame = out / "frames" / f"{step['step_id']}.json"
        frame.write_text("{}")
        rows.append({"step_id": step["step_id"], "status": "ok", "wall_ms": 2.0,
                     "frame": {"step_id": step["step_id"], "kind": "mesh",
                               "path": str(frame.relative_to(out)), "width": 0, "height": 0,
                               "camera": {}, "materials": [], "bbox": []},
                     "checkpoint": None, "metrics": {}, "reason": None})
    (out / "result.json").write_text(json.dumps({
        "engine": {"engine_id": "fake", "version": "0.0.1"}, "outcomes": rows}))
''')


def _job(steps: list[dict]) -> protocol.JobSpec:
    return protocol.JobSpec.from_json({
        "case_id": "unit", "engine": "fake",
        "domain": {"width": 0.1, "depth": 0.1, "height": 0.2, "grid_delta": 0.01},
        "initial": {"kind": "plane", "substrate": "Si", "substrate_thickness": 0.1,
                    "films": [{"material": "SiO2", "thickness": 0.02}]},
        "steps": steps,
    })


def _step(step_id: str, **overrides) -> dict:
    payload = {"step_id": step_id, "kind": "etch", "mechanism": "isotropic",
               "duration": 0.01, "rates": {"SiO2": -1.0}}
    payload.update(overrides)
    return payload


class ProtocolTests(unittest.TestCase):
    def test_roundtrip_preserves_every_field(self):
        job = _job([{"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    _step("s1", anisotropy=0.5, comparability="approximate")])
        again = protocol.JobSpec.from_json(job.to_json())
        self.assertEqual(job.to_json(), again.to_json())
        self.assertEqual(again.steps[1].anisotropy, 0.5)
        self.assertEqual(again.steps[1].comparability, "approximate")

    def test_duplicate_step_id_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            _job([_step("s1"), _step("s1")]).validate()

    def test_rate_step_needs_a_duration(self):
        with self.assertRaises(protocol.ProtocolError):
            _job([_step("s1", duration=0.0)]).validate()

    def test_non_numeric_rate_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            _job([_step("s1", rates={"SiO2": "fast"})]).validate()

    def test_schema_version_is_pinned(self):
        payload = _job([_step("s1")]).to_json()
        payload["schema_version"] = 99
        with self.assertRaises(protocol.ProtocolError):
            protocol.JobSpec.from_json(payload)

    def test_trench_needs_mask_material(self):
        payload = _job([_step("s1")]).to_json()
        payload["initial"] = {"kind": "trench", "substrate": "Si",
                              "substrate_thickness": 0.1, "films": [],
                              "trench_width": 0.05, "trench_depth": 0.05}
        with self.assertRaises(protocol.ProtocolError):
            protocol.JobSpec.from_json(payload)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_engine.py"
        self.script.write_text(FAKE_ENGINE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _registrations(self, ids=("a", "b")) -> dict:
        return {
            engine_id: EngineRegistration(
                engine_id=engine_id, label=engine_id,
                command=(sys.executable, str(self.script)),
                adapter="fake", mechanisms=("initial", "isotropic"),
                frame_kinds=("mesh",), env={})
            for engine_id in ids
        }

    def test_alignment_is_by_step_id_and_missing_steps_are_not_filled(self):
        job = _job([_step("s1"), _step("s2")])
        with mock.patch.object(scheduler, "registrations", lambda: self._registrations()):
            payload = scheduler.compare(job, ["a", "b"], self.root / "run")
        self.assertEqual(payload["summary"]["stepCount"], 2)
        self.assertEqual(payload["summary"]["engineCount"], 2)
        self.assertEqual(payload["summary"]["fullyAlignedSteps"], 2)
        self.assertEqual(payload["steps"][1]["engines"]["b"]["status"], "ok")

    def test_unsupported_is_reported_not_replaced(self):
        job = _job([_step("s1"), _step("s_unsupported")])
        with mock.patch.object(scheduler, "registrations", lambda: self._registrations()):
            payload = scheduler.compare(job, ["a", "b"], self.root / "run2")
        row = payload["steps"][1]
        self.assertFalse(row["aligned"])
        self.assertEqual(row["engines"]["a"]["status"], "unsupported")
        self.assertEqual(row["engines"]["a"]["reason"], "fake engine refuses")
        self.assertIsNone(row["engines"]["a"]["frame"])

    def test_missing_row_is_missing_never_ok(self):
        job = _job([_step("s_missing")])
        with mock.patch.object(scheduler, "registrations", lambda: self._registrations(["a"])):
            payload = scheduler.compare(job, ["a"], self.root / "run3")
        row = payload["steps"][0]
        self.assertEqual(row["engines"]["a"]["status"], "missing")
        self.assertFalse(row["aligned"])

    def test_digest_is_deterministic(self):
        job = _job([_step("s1")])
        first = scheduler._digest(job)
        second = scheduler._digest(protocol.JobSpec.from_json(job.to_json()))
        self.assertEqual(first, second)
        changed = _job([_step("s1", duration=0.02)])
        self.assertNotEqual(first, scheduler._digest(changed))

    def test_unknown_engine_is_refused(self):
        job = _job([_step("s1")])
        with mock.patch.object(scheduler, "registrations", lambda: self._registrations()):
            with self.assertRaises(SystemExit):
                scheduler.compare(job, ["nope"], self.root / "run4")


@unittest.skipUnless(os.environ.get("VIENNAPS_WORKBENCH_ENGINE_INTEGRATION") == "1",
                     "opt-in: needs ViennaPS and the SPX runtime + licence")
class IntegrationTests(unittest.TestCase):
    def test_viennaps_and_spx_align_on_the_shared_subset(self):
        job = _job([{"step_id": "s0", "kind": "initial", "mechanism": "initial"},
                    _step("s1")])
        with tempfile.TemporaryDirectory() as tmp:
            payload = scheduler.compare(job, ["viennaps", "spx"], Path(tmp))
        self.assertEqual(payload["summary"]["fullyAlignedSteps"], 2)
        self.assertEqual(payload["steps"][1]["engines"]["spx"]["frame"]["kind"], "raster")
        self.assertEqual(payload["steps"][1]["engines"]["viennaps"]["frame"]["kind"], "mesh")


if __name__ == "__main__":
    unittest.main()
