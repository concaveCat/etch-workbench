"""Opt-in GPU tests for the flat plasma plugins.

Run with ``WORKBENCH_GPU_TEST=1`` on a host with one visible CUDA device. The
tests use the shared plugin registry (now locked) and the v2 worker bridge, so
they exercise the same GPU profile resolution path as a real worker shadow.
CF4O2 is deliberately CPU-only: its flat constructor has no GPU model
conversion in the installed runtime, so the bridge must reject GPU_DISK instead
of falling back to CPU.
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
from tests.test_plugin_cf4o2 import cf4o2_project
from tests.test_plugin_cf4o2 import plane_domain as cf4o2_domain
from tests.test_plugin_fluorocarbon import (
    RAY_SEED as FLUOROCARBON_RAY_SEED,
)
from tests.test_plugin_fluorocarbon import (
    _reference as fluorocarbon_reference,
)
from tests.test_plugin_fluorocarbon import (
    fluorocarbon_project,
)
from tests.test_plugin_fluorocarbon import (
    plane_domain as fluorocarbon_domain,
)
from tests.test_plugin_hbro2 import (
    RAY_SEED as HBRO2_RAY_SEED,
)
from tests.test_plugin_hbro2 import (
    _reference as hbro2_reference,
)
from tests.test_plugin_hbro2 import (
    hbro2_project,
)
from tests.test_plugin_hbro2 import (
    plane_domain as hbro2_domain,
)
from tests.test_plugin_ionBeam import (
    RAY_SEED as ION_BEAM_RAY_SEED,
)
from tests.test_plugin_ionBeam import (
    _reference as ion_beam_reference,
)
from tests.test_plugin_ionBeam import (
    ion_beam_project,
)
from tests.test_plugin_ionBeam import (
    plane_domain as ion_beam_domain,
)
from tests.test_plugin_sf6c4f8 import (
    RAY_SEED as SF6C4F8_RAY_SEED,
)
from tests.test_plugin_sf6c4f8 import (
    _reference as sf6c4f8_reference,
)
from tests.test_plugin_sf6c4f8 import (
    plane_domain as sf6c4f8_domain,
)
from tests.test_plugin_sf6c4f8 import (
    sf6c4f8_project,
)
from worker.process_models_v2 import V2ProfileUnavailable, compare_domains, run_v2_step

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = ROOT / "var" / "test-plugin-plasma-gpu"


@unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires WORKBENCH_GPU_TEST=1")
class PlasmaPluginGpuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.device, cls.readback = acquire_gpu()

    def setUp(self) -> None:
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self, project: dict[str, Any], name: str, kernel: str, seed: int) -> SimpleNamespace:
        step = project["steps"][0]
        run_dir = (self.work_dir / name).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            project=project,
            ps=ps,
            ps_version=str(ps.__version__),
            native_engine_fingerprint="worker-v2-plugin-gpu",
            run_dir=run_dir,
            run_seed=seed,
            step_engines={step["id"]: kernel},
            selected_gpu_device=self.device,
            cuda_readback=self.readback,
        )

    def test_hbro2_gpu_disk_matches_direct_reference(self) -> None:
        project = hbro2_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "hbro2", "GPU_DISK", HBRO2_RAY_SEED)

        outcome = run_v2_step(ctx, step, hbro2_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_backend"], "gpu")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")
        self.assertEqual(outcome.plan.identity()["engine_device"], self.device["uuid"])

        reference = hbro2_reference(ps, project, hbro2_domain(2), 1e-3, kernel="GPU_DISK")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_sf6c4f8_gpu_disk_matches_direct_reference(self) -> None:
        project = sf6c4f8_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "sf6c4f8", "GPU_DISK", SF6C4F8_RAY_SEED)

        outcome = run_v2_step(ctx, step, sf6c4f8_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")

        reference = sf6c4f8_reference(ps, project, sf6c4f8_domain(2), 1e-3, kernel="GPU_DISK")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_fluorocarbon_gpu_disk_matches_direct_reference(self) -> None:
        project = fluorocarbon_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "fluorocarbon", "GPU_DISK", FLUOROCARBON_RAY_SEED)

        outcome = run_v2_step(ctx, step, fluorocarbon_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")

        reference = fluorocarbon_reference(
            ps,
            project,
            fluorocarbon_domain(2),
            step["nativeParameters"]["materialParametersByMaterial"],
            kernel="GPU_DISK",
        )
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_ion_beam_gpu_disk_matches_direct_reference(self) -> None:
        project = ion_beam_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "ionBeam", "GPU_DISK", ION_BEAM_RAY_SEED)

        outcome = run_v2_step(ctx, step, ion_beam_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")

        reference = ion_beam_reference(ps, project, ion_beam_domain(2), kernel="GPU_DISK")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_cf4o2_gpu_disk_is_rejected_before_the_native_call(self) -> None:
        project = cf4o2_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "cf4o2", "GPU_DISK", 271828)

        with self.assertRaises(V2ProfileUnavailable) as caught:
            run_v2_step(ctx, step, cf4o2_domain(2), allow_gpu=True)
        message = str(caught.exception)
        self.assertIn("cf4o2", message)
        self.assertIn("GPU_DISK", message)
