"""HTTP contract tests for the engine comparison routes.

Only validation and read paths are exercised here: a real comparison run spawns
engine subprocesses (seconds, plus an SPX licence seat) and belongs to the opt-in
integration test in ``tests/test_engines.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest

os.environ.setdefault("WORKBENCH_PORT", "8767")
os.environ.setdefault("VIENNAPS_WORKBENCH_DATA_DIR", "./var/test-compare-api")

from tests.test_api_integration import asgi_request  # noqa: E402


def _call(path: str, *, method: str = "GET", payload=None):
    status, headers, body = asyncio.run(asgi_request(path, method=method, payload=payload))
    return status, json.loads(body) if body else {}


JOB = {
    "case_id": "api-unit", "engine": "viennaps",
    "domain": {"width": 0.1, "depth": 0.1, "height": 0.2, "grid_delta": 0.01},
    "initial": {"kind": "plane", "substrate": "Si", "substrate_thickness": 0.1},
    "steps": [{"step_id": "s1", "kind": "etch", "mechanism": "isotropic",
               "duration": 0.01, "rates": {"Si": -1.0}}],
}


class CompareApiTests(unittest.TestCase):
    def test_engine_list_is_served(self):
        status, body = _call("/api/compare/engines")
        self.assertEqual(status, 200)
        ids = {row["engineId"] for row in body["engines"]}
        self.assertEqual(ids, {"viennaps", "spx"})

    def test_invalid_job_is_rejected_not_queued(self):
        bad = {**JOB, "steps": [{"step_id": "s1", "kind": "etch",
                                 "mechanism": "isotropic", "duration": 0.0,
                                 "rates": {"Si": -1.0}}]}
        status, body = _call("/api/compare/runs", method="POST",
                             payload={"job": bad, "engines": ["viennaps"]})
        self.assertEqual(status, 422)

    def test_unknown_engine_is_rejected(self):
        status, body = _call("/api/compare/runs", method="POST",
                             payload={"job": JOB, "engines": ["nope"]})
        self.assertEqual(status, 422)
        self.assertIn("unknown engine", json.dumps(body))

    def test_missing_run_is_404(self):
        status, body = _call("/api/compare/runs/" + "0" * 24)
        self.assertEqual(status, 404)

    def test_frame_path_traversal_is_refused(self):
        status, body = _call("/api/compare/runs/" + "0" * 24 + "/frames/../../etc/passwd")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
