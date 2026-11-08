"""Import-only guards against duplicated constants and facade drift.

These tests do not need native ViennaPS, CUDA, or a running server.
"""

from __future__ import annotations

import unittest

from backend import main as backend_main
from backend import planning, supervisor, validation
from backend.api import runs as api_runs


class StrategyConstantParityTest(unittest.TestCase):
    def test_worker_strategy_constants_match_validation(self):
        from worker import simulate

        for name in ("SPLIT_STRATEGY", "FLUX_STRATEGY", "NATIVE_STRATEGY", "NATIVE_FLUX_STRATEGY"):
            full = f"GPU_MULTIPARTICLE_{name}"
            with self.subTest(constant=full):
                self.assertEqual(getattr(simulate, full), getattr(validation, full))


class FacadeParityTest(unittest.TestCase):
    def test_supervisor_replay_mismatch_is_planning_class(self):
        self.assertIs(supervisor.ReplayMismatch, planning.ReplayMismatch)

    def test_main_re_exports_run_routes(self):
        self.assertIs(backend_main.run_download, api_runs.run_download)
        self.assertIs(backend_main.intermediate_output_get, api_runs.intermediate_output_get)


if __name__ == "__main__":
    unittest.main()
