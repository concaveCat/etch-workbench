"""Shadow/apply tests for the worker's geometry step hook.

Both flags are default-off. The geometry hook reuses the same generic v2
bridge as process steps, but runs the shared ``GeometryEngine`` operations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import viennals
import viennaps as ps

import worker.simulate as simulate
from process_models.plugins import load_registry
from tests.test_worker_v2_isotropic import TEST_ROOT, trench_domain
from worker.process_models_v2 import find_v2_process
from worker.steps.context import RunContext
from worker.steps.geometry import V2_PLUGIN_APPLY_ENV, V2_PLUGIN_SHADOW_ENV, run_geometry_step

ROOT = Path(__file__).resolve().parents[1]


def _project() -> dict[str, Any]:
    return {
        "schemaVersion": 5,
        "name": "geometry shadow",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {
            "kind": "trench",
            "width": 1.6,
            "depth": 1.2,
            "taperAngle": 0.0,
            "maskHeight": 0.6,
            "maskTaperAngle": 0.0,
            "substrateMaterial": "Si",
            "maskMaterial": "Mask",
        },
        "steps": [],
        "solver": {
            "timeStepRatio": 0.25,
            "raysPerPoint": 32,
            "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 314159},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def _planarize_step() -> dict[str, Any]:
    return {
        "id": "planarize_step_01",
        "name": "cmp",
        "enabled": True,
        "model": "planarize",
        "cutoffHeight": 0.1,
    }


def _events(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    path = run_dir / "events.ndjson"
    if not path.is_file():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [record for record in records if record.get("type") == event_type]


@contextlib.contextmanager
def _v2_flags(*, shadow: bool = False, apply: bool = False):
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in (V2_PLUGIN_SHADOW_ENV, V2_PLUGIN_APPLY_ENV):
            os.environ.pop(name, None)
        if shadow:
            os.environ[V2_PLUGIN_SHADOW_ENV] = "1"
        if apply:
            os.environ[V2_PLUGIN_APPLY_ENV] = "1"
        yield


class GeometryShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.setDimension(2)
        self.project = _project()
        self.work_dir = TEST_ROOT / f"geometry-{self._testMethodName}"
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(
        self,
        name: str,
        step: dict[str, Any],
        domain: Any | None = None,
        *,
        material_tops: dict[str, Any] | None = None,
    ) -> RunContext:
        run_dir = self.work_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunContext(
            project=self.project,
            ps=ps,
            domain=domain or trench_domain(),
            run_dir=run_dir,
            run_id=f"run-{name}",
            config={},
            actual_steps=[step],
            step_engines={},
            engine_preference="cpu",
            run_seed=None,
            selected_gpu_device=None,
            cuda_readback=None,
            gpu_sample=None,
            native_engine_fingerprint=None,
            ps_version=str(ps.__version__),
            ls_version=str(viennals.__version__),
            snapshots=[{"stepId": "initial", "materialTops": material_tops}],
            field_state={},
            actual_execution=[],
            intermediate_outputs=[],
        )

    def test_flag_off_keeps_the_worker_path(self) -> None:
        step = _planarize_step()
        ctx = self._context("off", step)
        original = ctx.domain
        before = simulate.surface_state(ctx.domain, 2)["digest"]

        with _v2_flags():
            run_geometry_step(ctx, 1, step)

        self.assertIs(ctx.domain, original)
        self.assertNotEqual(simulate.surface_state(ctx.domain, 2)["digest"], before)
        self.assertEqual(_events(ctx.run_dir, "v2-plugin-shadow"), [])
        self.assertFalse((ctx.run_dir / "v2").exists())

    def test_shadow_compares_the_v2_geometry_engine(self) -> None:
        step = _planarize_step()
        ctx = self._context("shadow", step)
        original = ctx.domain

        with _v2_flags(shadow=True):
            run_geometry_step(ctx, 1, step)

        self.assertIs(ctx.domain, original)
        events = _events(ctx.run_dir, "v2-plugin-shadow")
        self.assertEqual(len(events), 1, events)
        shadow = events[0]
        self.assertTrue(shadow["matched"], shadow["reasons"])
        self.assertEqual(shadow["oldDigest"], shadow["newDigest"])
        self.assertEqual(shadow["oldMaterialMap"], shadow["newMaterialMap"])
        self.assertEqual(shadow["v2Plan"]["process_id"], "planarize")
        self.assertEqual(shadow["v2Plan"]["engine_id"], "viennaps")
        self.assertEqual(shadow["v2Metadata"]["geometryOperation"], "planarize")
        self.assertEqual(_events(ctx.run_dir, "v2-plugin-applied"), [])

    def test_apply_uses_the_validated_v2_domain(self) -> None:
        step = _planarize_step()
        ctx = self._context("apply", step)
        original = ctx.domain

        with _v2_flags(apply=True):
            run_geometry_step(ctx, 1, step)

        self.assertIsNot(ctx.domain, original)
        shadow = _events(ctx.run_dir, "v2-plugin-shadow")
        applied = _events(ctx.run_dir, "v2-plugin-applied")
        self.assertEqual(len(shadow), 1)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["v2Plan"], shadow[0]["v2Plan"])
        self.assertEqual(applied[0]["newDigest"], shadow[0]["newDigest"])

    def test_stop_material_planarize_shadows_with_previous_tops(self) -> None:
        step = {
            "id": "planarize_stop_01",
            "name": "cmp stop",
            "enabled": True,
            "model": "planarize",
            "stopMaterial": "Mask",
            "overPolish": 0.05,
        }
        ctx = self._context(
            "shadow-stop",
            step,
            material_tops={"Mask": {"p90TopHeight": 0.6, "medianTopHeight": 0.6}},
        )
        with (
            _v2_flags(shadow=True),
            mock.patch.object(ctx, "save_snapshot", return_value={}),
            mock.patch.object(ctx, "save_native_state", return_value={}),
        ):
            run_geometry_step(ctx, 1, step)
        events = _events(ctx.run_dir, "v2-plugin-shadow")
        self.assertEqual(len(events), 1, events)
        self.assertTrue(events[0]["matched"], events[0]["reasons"])
        self.assertEqual(events[0]["v2Metadata"]["cutoffMode"], "material-aware")
        self.assertAlmostEqual(events[0]["v2Metadata"]["measuredStopTopHeight"], 0.6)
        self.assertAlmostEqual(events[0]["v2Metadata"]["cutoffHeight"], 0.55)

    def test_gds_boolean_shadows_the_worker_operand(self) -> None:
        from tests.test_gds_boolean import FIXTURE, gds_project, mask_step

        work = self.work_dir / "gds"
        work.mkdir()
        asset = work / "multi-layer.gds"
        shutil.copyfile(FIXTURE, asset)
        project = gds_project(asset_id=hashlib.sha256(asset.read_bytes()).hexdigest())
        step = mask_step()
        ps.setDimension(3)
        domain = ps.Domain(0.1, 4.0, 4.0, boundary=ps.BoundaryType.REFLECTIVE_BOUNDARY)
        simulate.make_v3_geometry(
            ps,
            domain,
            project,
            config={"gdsAssetPath": str(asset)},
            run_dir=work,
        )
        ctx = RunContext(
            project=project,
            ps=ps,
            domain=domain,
            run_dir=work,
            run_id="run-gds-shadow",
            config={"gdsAssetPath": str(asset)},
            actual_steps=[step],
            step_engines={},
            engine_preference="cpu",
            run_seed=None,
            selected_gpu_device=None,
            cuda_readback=None,
            gpu_sample=None,
            native_engine_fingerprint=None,
            ps_version=str(ps.__version__),
            ls_version=str(viennals.__version__),
            snapshots=[{"stepId": "initial", "materialTops": None}],
            field_state={},
            actual_execution=[],
            intermediate_outputs=[],
        )
        with (
            _v2_flags(shadow=True),
            mock.patch.object(ctx, "save_snapshot", return_value={}),
            mock.patch.object(ctx, "save_native_state", return_value={}),
        ):
            run_geometry_step(ctx, 1, step)
        events = _events(ctx.run_dir, "v2-plugin-shadow")
        self.assertEqual(len(events), 1, events)
        self.assertTrue(events[0]["matched"], events[0]["reasons"])
        self.assertEqual(events[0]["v2Plan"]["process_id"], "gdsBoolean")
        self.assertEqual(events[0]["v2Metadata"]["geometryOperation"], "gdsBoolean")

    def test_unclaimed_geometry_step_records_a_skip(self) -> None:
        step = {"id": "unknown_step_01", "name": "unknown", "enabled": True, "model": "unknownGeometry"}
        ctx = self._context("skip", step)
        with mock.patch.object(simulate, "apply_geometry_operation", return_value={}):
            with _v2_flags(shadow=True):
                run_geometry_step(ctx, 1, step)
        skipped = _events(ctx.run_dir, "v2-plugin-shadow-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("unknownGeometry", skipped[0]["reason"])

    def test_find_v2_process_sees_geometry_specs(self) -> None:
        registry = load_registry()
        spec = find_v2_process(self.project, _planarize_step(), registry=registry)
        # find_v2_process alone does not register built-ins; the geometry hook
        # registers them before dispatch.
        self.assertIsNone(spec)
        from process_models.v2.geometry import register_geometry

        register_geometry(registry)
        spec = find_v2_process(self.project, _planarize_step(), registry=registry)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.id, "planarize")
        self.assertEqual(spec.execution_kind, "geometry")


if __name__ == "__main__":
    unittest.main()
