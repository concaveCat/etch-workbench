from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend.models import ReplayRequirements, Solver
from backend.runtime_limits import available_cpu_threads, cpu_thread_limit


class CpuThreadLimitTests(unittest.TestCase):
    def test_affinity_is_the_runtime_limit(self) -> None:
        with patch("backend.runtime_limits.os.sched_getaffinity", return_value={2, 5, 9}):
            self.assertEqual(cpu_thread_limit(), {"maximum": 3, "limitSource": "process CPU affinity"})

    def test_cpu_count_fallback_is_used_if_affinity_is_unavailable(self) -> None:
        with patch("backend.runtime_limits.os.sched_getaffinity", side_effect=OSError("unavailable")), \
             patch("backend.runtime_limits.os.cpu_count", return_value=7):
            self.assertEqual(cpu_thread_limit(), {"maximum": 7, "limitSource": "os.cpu_count fallback"})
        with patch("backend.runtime_limits.os.sched_getaffinity", side_effect=OSError("unavailable")), \
             patch("backend.runtime_limits.os.cpu_count", return_value=None):
            self.assertEqual(available_cpu_threads(), 1)

    def test_solver_and_replay_enforce_the_same_runtime_limit(self) -> None:
        maximum = available_cpu_threads()
        solver = {
            "timeStepRatio": 0.25,
            "raysPerPoint": 32,
            "cpuThreads": maximum,
            "random": {"mode": "fixed", "seed": 314159},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        }
        Solver.model_validate(solver)
        replay = {"viennapsVersion": "4.6.2", "viennalsVersion": "5.8.5", "cpuThreads": maximum, "steps": []}
        ReplayRequirements.model_validate(replay)

        solver["cpuThreads"] = maximum + 1
        with self.assertRaises(ValidationError):
            Solver.model_validate(solver)
        replay["cpuThreads"] = maximum + 1
        with self.assertRaises(ValidationError):
            ReplayRequirements.model_validate(replay)


if __name__ == "__main__":
    unittest.main()
