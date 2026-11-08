"""Opt-in GPU tests for TEOS deposition plugins.

TEOSPECVD has a GPU_DISK profile and is compared with a direct GPU reference.
TEOSDeposition deliberately has no GPU profile because the flat constructor has
no GPU model conversion in the installed runtime; the bridge must reject it
instead of running the CPU fallback.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import viennaps as ps

from tests.gpu_support import acquire_gpu
from tests.test_plugin_teosDeposition import plane_domain as teos_domain
from tests.test_plugin_teosDeposition import teos_project
from tests.test_plugin_teospecvd import RAY_SEED, teospecvd_project
from tests.test_plugin_teospecvd import _reference as teospecvd_reference
from tests.test_plugin_teospecvd import plane_domain as teospecvd_domain
from worker.process_models_v2 import V2ProfileUnavailable, compare_domains, run_v2_step

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-deposition-gpu"


@unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires WORKBENCH_GPU_TEST=1")
class DepositionGpuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.device, cls.readback = acquire_gpu()

    def setUp(self) -> None:
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self, project: dict[str, Any], name: str) -> SimpleNamespace:
        step = project["steps"][0]
        run_dir = (self.work_dir / name).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            project=project,
            ps=ps,
            ps_version=str(ps.__version__),
            native_engine_fingerprint="worker-v2-plugin-gpu",
            run_dir=run_dir,
            run_seed=RAY_SEED,
            step_engines={step["id"]: "GPU_DISK"},
            selected_gpu_device=self.device,
            cuda_readback=self.readback,
        )

    def test_teospecvd_gpu_disk_matches_direct_reference(self) -> None:
        project = teospecvd_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "teospecvd")

        outcome = run_v2_step(ctx, step, teospecvd_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_backend"], "gpu")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")

        reference = teospecvd_reference(ps, project, teospecvd_domain(2), kernel="GPU_DISK")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_teos_deposition_gpu_disk_is_rejected_before_the_native_call(self) -> None:
        project = teos_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "teosDeposition")

        with self.assertRaises(V2ProfileUnavailable) as caught:
            run_v2_step(ctx, step, teos_domain(2), allow_gpu=True)
        message = str(caught.exception)
        self.assertIn("teosDeposition", message)
        self.assertIn("GPU_DISK", message)
