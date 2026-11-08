"""GPU/GPU_TRIANGLE execution tests for the native SF6/O2 plugin.

These tests are opt-in because they exercise the host GPU. They run only when
``WORKBENCH_GPU_TEST=1`` and a CUDA device is visible. The default suite keeps
the CPU path unchanged.

The GPU_TRIANGLE reference uses the archived Stage 9 evidence project, which is
not part of the git tree. When that evidence directory is absent the test is
skipped; the evidence gate itself is still tested by
``test_gpu_triangle_plane_is_rejected_without_evidence``.
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import viennaps as ps

import worker.simulate as simulate
from process_models.v2.engines.viennaps import configure_advection, configure_rays
from tests.gpu_support import acquire_gpu
from tests.test_plugin_sf6o2 import (
    GRID_DELTA,
    RAY_SEED,
    TEST_ROOT,
    plane_domain,
    sf6o2_project,
)
from worker.process_models_v2 import V2ProfileUnavailable, compare_domains, run_v2_step

GPU_EVIDENCE = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "stage9-evidence"
    / "gpu-variants"
    / "gpu-triangle-2d"
    / "sf6o2-2d-gpu_triangle.final.input.json"
)


def _direct_gpu_reference(project: dict[str, Any], domain: Any, kernel: str) -> Any:
    step = project["steps"][0]
    values = step["nativeParameters"]
    model = ps.SF6O2Etching(
        values["ionFlux"],
        values["etchantFlux"],
        values["oxygenFlux"],
        values["meanIonEnergy"],
        values["sigmaIonEnergy"],
        values["ionExponent"],
        values["oxySputterYield"],
        values["etchStopDepth"],
    )
    process = ps.Process(domain, model, float(step["duration"]))
    process.setFluxEngineType(getattr(ps.FluxEngineType, kernel))
    advection, _actual = configure_advection(ps, project)
    process.setParameters(advection)
    rays, _ray_actual = configure_rays(ps, project, RAY_SEED)
    process.setParameters(rays)
    coverage = ps.CoverageParameters()
    coverage.tolerance = 1e-3
    process.setParameters(coverage)
    process.apply()
    return domain


def _stack_domain(project: dict[str, Any]) -> Any:
    ps.setDimension(2)
    ps.Length.setUnit("um")
    ps.Time.setUnit("s")
    domain = ps.Domain(
        gridDelta=GRID_DELTA,
        xExtent=4.0,
        boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY,
    )
    simulate.make_v3_geometry(ps, domain, project)
    return domain


@unittest.skipUnless(os.environ.get("WORKBENCH_GPU_TEST") == "1", "requires WORKBENCH_GPU_TEST=1")
class SF6O2GpuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.device, cls.readback = acquire_gpu()

    def setUp(self) -> None:
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self, project: dict[str, Any], name: str, kernel: str) -> SimpleNamespace:
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
            step_engines={step["id"]: kernel},
            selected_gpu_device=self.device,
            cuda_readback=self.readback,
        )

    def test_gpu_disk_matches_direct_reference(self) -> None:
        project = sf6o2_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_DISK"
        step = project["steps"][0]
        ctx = self._context(project, "gpu-disk", "GPU_DISK")

        outcome = run_v2_step(ctx, step, plane_domain(2), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_backend"], "gpu")
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_DISK")
        self.assertEqual(outcome.plan.identity()["engine_device"], self.device["uuid"])

        reference = _direct_gpu_reference(project, plane_domain(2), "GPU_DISK")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_gpu_triangle_matches_direct_reference(self) -> None:
        if not GPU_EVIDENCE.is_file():
            self.skipTest("archived GPU_TRIANGLE evidence is not available in this worktree")
        project = json.loads(GPU_EVIDENCE.read_text(encoding="utf-8"))
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_TRIANGLE"
        step = next(item for item in project["steps"] if item.get("nativeModel") == "sf6o2")
        ctx = self._context(project, "gpu-triangle", "GPU_TRIANGLE")

        outcome = run_v2_step(ctx, step, _stack_domain(project), allow_gpu=True)
        self.assertEqual(outcome.plan.identity()["engine_kernel"], "GPU_TRIANGLE")

        reference = _direct_gpu_reference(project, _stack_domain(project), "GPU_TRIANGLE")
        comparison = compare_domains(reference, outcome.state.geometry.handle, 2)
        self.assertTrue(comparison["matched"], comparison["reasons"])

    def test_gpu_triangle_plane_is_rejected_without_evidence(self) -> None:
        project = sf6o2_project(2)
        project["solver"]["enginePreference"] = "gpu"
        project["solver"]["gpuEngine"] = "GPU_TRIANGLE"
        step = project["steps"][0]
        ctx = self._context(project, "gpu-triangle-plane", "GPU_TRIANGLE")

        with self.assertRaises(V2ProfileUnavailable) as caught:
            run_v2_step(ctx, step, plane_domain(2), allow_gpu=True)
        self.assertIn("GPU_TRIANGLE", str(caught.exception))
        self.assertIn("direct-reference acceptance row", str(caught.exception))
