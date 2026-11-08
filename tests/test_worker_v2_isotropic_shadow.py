"""Shadow/apply hook tests for the generic v2 plugin hook.

Both hooks are default-off. These tests pin every side:

* with both env flags unset, ``run_process_step`` behaves exactly as before
  (no copy, no shadow event, no v2 metadata, same domain object);
* with ``VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW=1``, a step claimed by a
  registered plugin runs the v2 path on a deep copy and records a
  ``v2-plugin-shadow`` event whose digests and material map match the worker
  path, while the returned metadata and the worker domain do not change;
* with ``VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY=1`` that validated v2 domain
  becomes ``ctx.domain`` (snapshots/native state/replay then describe the v2
  result) and a ``v2-plugin-applied`` event records the digests, plan and
  metadata. Apply implies shadow; an unvalidated apply does not exist.

Steps no plugin claims (material rates, geometric/auto-geometric, or a model
without a plugin) must record a ``v2-plugin-shadow-skipped`` event and never
call the bridge, with or without the apply flag.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import viennals
import viennaps as ps

import worker.simulate as simulate
from process_models.plugins import load_registry
from tests.test_worker_v2_isotropic import (
    GRID_DELTA,
    TEST_ROOT,
    _old_reference,
    bridge_ctx,
    isotropic_step,
    plane_domain,
    plane_project,
)
from worker.process_models_v2 import run_v2_step
from worker.steps.context import RunContext
from worker.steps.process import (
    V2_PLUGIN_APPLY_ENV,
    V2_PLUGIN_SHADOW_ENV,
    _v2_plugin_skip_reason,
    run_process_step,
)


def _worker_project() -> dict[str, Any]:
    """Complete the bridge fixture so the worker's native-state write can parse it."""
    project = plane_project(2)
    project["solver"].update(
        {
            "random": {"mode": "fixed", "seed": 314159},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        }
    )
    project["view"] = {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True}
    return project


def _top(domain: Any) -> float:
    mesh = domain.getSurfaceMesh(addInterfaces=True, sharpCorners=False, minNodeDistanceFactor=0.01)
    return max(float(node[1]) for node in mesh.getNodes())


def _events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.ndjson"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _events_of(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    return [record for record in _events(run_dir) if record.get("type") == event_type]


@contextlib.contextmanager
def _v2_flags(*, shadow: bool = False, apply: bool = False):
    """Set or clear *both* hooks' env flags for the duration of a call."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in (V2_PLUGIN_SHADOW_ENV, V2_PLUGIN_APPLY_ENV):
            os.environ.pop(name, None)
        if shadow:
            os.environ[V2_PLUGIN_SHADOW_ENV] = "1"
        if apply:
            os.environ[V2_PLUGIN_APPLY_ENV] = "1"
        yield


@contextlib.contextmanager
def _shadow_flag(enabled: bool):
    """Set or clear only the shadow flag for the duration of a call."""
    with _v2_flags(shadow=enabled):
        yield


class _V2WorkerStepTestBase(unittest.TestCase):
    """Shared fixtures; no tests live here so the base class is not collected."""

    def setUp(self) -> None:
        ps.setNumThreads(1)
        ps.Length.setUnit("um")
        ps.setDimension(2)
        self.project = _worker_project()
        self.work_dir = TEST_ROOT / self._testMethodName
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _context(self, name: str, step: dict[str, Any]) -> RunContext:
        run_dir = self.work_dir / name
        run_dir.mkdir(parents=True, exist_ok=True)
        return RunContext(
            project=self.project,
            ps=ps,
            domain=plane_domain(2),
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
            # run_process_step reads ctx.snapshots[-1] for the measured step
            # change; a real run always has the initial snapshot first.
            snapshots=[{"stepId": "initial", "materialTops": None}],
            field_state={},
            actual_execution=[],
            intermediate_outputs=[],
        )

    def _assert_no_v2_metadata(self, metadata: dict[str, Any]) -> None:
        self.assertEqual([key for key in metadata if "v2" in key.lower()], [])

    def _assert_shadow_matches(self, run_dir: Path) -> dict[str, Any]:
        records = _events_of(run_dir, "v2-plugin-shadow")
        self.assertEqual(len(records), 1, records)
        shadow = records[0]
        self.assertIs(shadow["matched"], True, shadow["reasons"])
        self.assertEqual(shadow["reasons"], [])
        self.assertEqual(shadow["oldDigest"], shadow["newDigest"])
        self.assertEqual(shadow["oldMaterialMap"], shadow["newMaterialMap"])
        self.assertEqual(shadow["oldPointCount"], shadow["newPointCount"])
        self.assertEqual(shadow["oldCellCount"], shadow["newCellCount"])
        self.assertIsInstance(shadow["v2Warnings"], list)
        self.assertIsInstance(shadow["v2Metadata"], dict)
        plan = shadow["v2Plan"]
        self.assertEqual(plan["process_id"], "isotropic")
        self.assertEqual(plan["variant_id"], "native")
        self.assertEqual(plan["engine_id"], "viennaps")
        self.assertEqual(plan["engine_version"], str(ps.__version__))
        self.assertEqual(plan["engine_fingerprint"], "worker-v2-plugin")
        return shadow

    def _assert_applied_event_matches(self, run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        """The apply record must carry the validated shadow's digests/plan/metadata."""
        shadow = self._assert_shadow_matches(run_dir)
        applied = _events_of(run_dir, "v2-plugin-applied")
        self.assertEqual(len(applied), 1, applied)
        record = applied[0]
        self.assertEqual(record["stepId"], shadow["stepId"])
        self.assertEqual(record["oldDigest"], shadow["oldDigest"])
        self.assertEqual(record["newDigest"], shadow["newDigest"])
        self.assertEqual(record["oldDigest"], record["newDigest"])
        self.assertEqual(record["v2Plan"], shadow["v2Plan"])
        self.assertEqual(record["v2Metadata"], shadow["v2Metadata"])
        return shadow, record


class V2PluginShadowTests(_V2WorkerStepTestBase):
    def test_flag_off_keeps_the_default_path(self) -> None:
        step = isotropic_step("etch", rate=0.3, duration=1.0)
        ctx = self._context("flag-off", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _v2_flags():
            result = run_process_step(ctx, 1, step)

        self.assertIs(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.3, delta=1e-6)
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow"), [])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-applied"), [])
        self.assertEqual(len(_events_of(ctx.run_dir, "step-configured")), 1)
        # Default off: the bridge is not imported by the step module at all.
        self.assertFalse((ctx.run_dir / "v2").exists())

    def test_flag_on_etch_shadows_a_copy_and_matches_the_worker(self) -> None:
        step = isotropic_step("etch", rate=0.3, duration=1.0, ratio=0.25)
        ctx = self._context("flag-on-etch", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _shadow_flag(True):
            result = run_process_step(ctx, 1, step)

        # The worker domain moved exactly once; the shadow ran on a copy.
        self.assertIs(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.3, delta=1e-6)
        shadow = self._assert_shadow_matches(ctx.run_dir)
        self.assertEqual(simulate.surface_state(ctx.domain, 2)["digest"], shadow["oldDigest"])
        self.assertEqual(shadow["v2Metadata"]["operation"], "etch")
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])
        # Shadow alone never applies.
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-applied"), [])

    def test_flag_on_deposit_shadows_a_copy_and_matches_the_worker(self) -> None:
        step = isotropic_step("deposit", rate=0.3, duration=1.0)
        ctx = self._context("flag-on-deposit", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _shadow_flag(True):
            result = run_process_step(ctx, 1, step)

        self.assertIs(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, 0.3, delta=1e-6)
        shadow = self._assert_shadow_matches(ctx.run_dir)
        self.assertEqual(simulate.surface_state(ctx.domain, 2)["digest"], shadow["oldDigest"])
        self.assertIn(int(ps.Material.SiO2), shadow["newMaterialMap"])
        self.assertEqual(shadow["v2Metadata"]["depositMaterial"], "SiO2")
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-applied"), [])

    def test_snapshot_geometry_matches_the_worker_path(self) -> None:
        """The v2 domain must produce the same snapshot JSON geometry as the old path."""
        for operation in ("etch", "deposit"):
            with self.subTest(operation=operation):
                step = isotropic_step(operation, rate=0.3, duration=1.0)
                old_domain = plane_domain(2)
                new_domain = plane_domain(2)
                _old_reference(ps, self.project, step, old_domain)
                outcome = run_v2_step(
                    bridge_ctx(self.project, self.work_dir / f"v2-snapshot-{operation}"),
                    step,
                    new_domain,
                )
                new_domain = outcome.state.geometry.handle

                old_dir = self.work_dir / f"old-snapshot-{operation}"
                new_dir = self.work_dir / f"new-snapshot-{operation}"
                old_dir.mkdir()
                new_dir.mkdir()
                for domain, directory in ((old_domain, old_dir), (new_domain, new_dir)):
                    simulate.save_snapshot(
                        ps,
                        domain,
                        directory,
                        run_id="snapshot-parity",
                        step_id=step["id"],
                        step_name=step["name"],
                        step_index=1,
                        requested=self.project,
                        step=step,
                    )

                old_payload = json.loads((old_dir / f"{step['id']}.json").read_text(encoding="utf-8"))
                new_payload = json.loads((new_dir / f"{step['id']}.json").read_text(encoding="utf-8"))
                # Paths are run-directory relative and therefore not part of the
                # geometry contract; every other field (points, topology, material
                # data, measured tops/change, readback) must be identical.
                ignored = {"outputs"}
                self.assertEqual(
                    {key: value for key, value in old_payload.items() if key not in ignored},
                    {key: value for key, value in new_payload.items() if key not in ignored},
                )

    def test_native_state_hash_matches_the_worker_path(self) -> None:
        """Replay identity starts with the persisted native state file."""
        for operation in ("etch", "deposit"):
            with self.subTest(operation=operation):
                step = isotropic_step(operation, rate=0.3, duration=1.0)
                old_domain = plane_domain(2)
                new_domain = plane_domain(2)
                _old_reference(ps, self.project, step, old_domain)
                outcome = run_v2_step(
                    bridge_ctx(self.project, self.work_dir / f"v2-state-{operation}"),
                    step,
                    new_domain,
                )
                new_domain = outcome.state.geometry.handle

                old_dir = self.work_dir / f"old-state-{operation}"
                new_dir = self.work_dir / f"new-state-{operation}"
                old_dir.mkdir()
                new_dir.mkdir()
                old_state = simulate.save_native_state(
                    ps,
                    old_domain,
                    old_dir,
                    project=self.project,
                    run_id="state-parity",
                    step_id=step["id"],
                    viennaps_version=str(ps.__version__),
                    viennals_version=str(viennals.__version__),
                )
                new_state = simulate.save_native_state(
                    ps,
                    new_domain,
                    new_dir,
                    project=self.project,
                    run_id="state-parity",
                    step_id=step["id"],
                    viennaps_version=str(ps.__version__),
                    viennals_version=str(viennals.__version__),
                )
                self.assertEqual(old_state["stateId"], new_state["stateId"])

    def test_registry_is_loaded_once_per_step_and_shared_with_the_bridge(self) -> None:
        step = isotropic_step("etch", rate=0.3, duration=1.0)
        ctx = self._context("flag-on-load-once", step)
        calls: list[tuple[Any, ...]] = []

        def counting_loader(*args: Any, **kwargs: Any):
            calls.append((args, kwargs))
            return load_registry(*args, **kwargs)

        with _shadow_flag(True), mock.patch("process_models.plugins.load_registry", side_effect=counting_loader):
            run_process_step(ctx, 1, step)

        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])

    def test_gpu_profile_is_skipped_not_downgraded(self) -> None:
        step = isotropic_step("etch", rate=0.3, duration=1.0)
        ctx = self._context("flag-on-gpu", step)
        ctx.step_engines[step["id"]] = "GPU_DISK"
        before = _top(ctx.domain)

        with _shadow_flag(True):
            run_process_step(ctx, 1, step)

        # The worker path still runs; v2 must not silently run the same step on
        # CPU and call it a match.
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.3, delta=1e-6)
        skipped = _events_of(ctx.run_dir, "v2-plugin-shadow-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertEqual(skipped[0]["stepId"], step["id"])
        self.assertIn("GPU_DISK", skipped[0]["reason"])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow"), [])
        self.assertFalse((ctx.run_dir / "v2").exists())

    def test_material_rate_mode_shadows_a_copy_and_matches_the_worker(self) -> None:
        step = isotropic_step("etch", rate=0.0, duration=1.0)
        step["rateMode"] = "material"
        step["materialRates"] = {"Si": 0.2}
        ctx = self._context("flag-on-material", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _shadow_flag(True):
            result = run_process_step(ctx, 1, step)

        self.assertIs(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.2, delta=1e-6)
        shadow = self._assert_shadow_matches(ctx.run_dir)
        self.assertEqual(shadow["v2Metadata"]["rateMode"], "material")
        self.assertAlmostEqual(shadow["v2Metadata"]["nativeDefaultRate"], 0.0, delta=1e-12)
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-applied"), [])

    def test_geometric_mode_is_skipped_without_a_bridge_call(self) -> None:
        # Thickness 0.3 um is >= gridDelta 0.2 um, so the explicit geometric
        # path is a legal request and the worker really runs it.
        self.assertGreaterEqual(0.3, GRID_DELTA)
        step = isotropic_step("etch", rate=0.3, duration=1.0)
        step["isotropicMethod"] = "geometric"
        ctx = self._context("flag-on-geometric", step)
        before = _top(ctx.domain)

        with _shadow_flag(True), mock.patch("worker.process_models_v2.run_v2_step") as bridge:
            run_process_step(ctx, 1, step)

        bridge.assert_not_called()
        self.assertLess(_top(ctx.domain) - before, -0.1)
        skipped = _events_of(ctx.run_dir, "v2-plugin-shadow-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("geometric", skipped[0]["reason"].lower())
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow"), [])

    def test_model_without_a_plugin_is_skipped(self) -> None:
        step = isotropic_step("etch", rate=0.0, duration=1.0)
        step["model"] = "directional"
        step["direction"] = [0.0, -1.0, 0.0]
        step["directionalRateMagnitude"] = 0.0
        step["isotropicRateMagnitude"] = 0.0
        step["calculateVisibility"] = False
        ctx = self._context("flag-on-unclaimed", step)

        with _shadow_flag(True), mock.patch("worker.process_models_v2.run_v2_step") as bridge:
            run_process_step(ctx, 1, step)

        bridge.assert_not_called()
        skipped = _events_of(ctx.run_dir, "v2-plugin-shadow-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("directional", skipped[0]["reason"])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow"), [])


class V2PluginApplyTests(_V2WorkerStepTestBase):
    """Validation-only apply: an eligible, matched v2 domain replaces ctx.domain."""

    def test_apply_etch_replaces_the_domain_with_the_v2_domain(self) -> None:
        step = isotropic_step("etch", rate=0.3, duration=1.0, ratio=0.25)
        ctx = self._context("apply-etch", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _v2_flags(apply=True):
            result = run_process_step(ctx, 1, step)

        self.assertIsNot(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.3, delta=1e-6)
        # Apply implies the shadow comparison; the applied record reuses it.
        shadow, applied = self._assert_applied_event_matches(ctx.run_dir)
        self.assertEqual(shadow["v2Metadata"]["operation"], "etch")
        # Snapshots/native state now describe the v2 domain.
        self.assertEqual(simulate.surface_state(ctx.domain, 2)["digest"], applied["newDigest"])
        # The returned metadata still comes from the old save_snapshot call.
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])

    def test_apply_deposit_keeps_the_deposited_material(self) -> None:
        step = isotropic_step("deposit", rate=0.3, duration=1.0)
        ctx = self._context("apply-deposit", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _v2_flags(apply=True):
            result = run_process_step(ctx, 1, step)

        self.assertIsNot(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, 0.3, delta=1e-6)
        self.assertIn(int(ps.Material.SiO2), simulate.material_map(ctx.domain))
        shadow, applied = self._assert_applied_event_matches(ctx.run_dir)
        self.assertEqual(shadow["v2Metadata"]["depositMaterial"], "SiO2")
        self.assertEqual(simulate.surface_state(ctx.domain, 2)["digest"], applied["newDigest"])
        self._assert_no_v2_metadata(result.metadata)

    def test_apply_accepts_material_rate_mode(self) -> None:
        step = isotropic_step("etch", rate=0.0, duration=1.0)
        step["rateMode"] = "material"
        step["materialRates"] = {"Si": 0.2}
        ctx = self._context("apply-material", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _v2_flags(apply=True):
            result = run_process_step(ctx, 1, step)

        self.assertIsNot(ctx.domain, original_domain)
        self.assertAlmostEqual(_top(ctx.domain) - before, -0.2, delta=1e-6)
        shadow, applied = self._assert_applied_event_matches(ctx.run_dir)
        self.assertEqual(shadow["v2Metadata"]["rateMode"], "material")
        self.assertEqual(simulate.surface_state(ctx.domain, 2)["digest"], applied["newDigest"])
        self._assert_no_v2_metadata(result.metadata)
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow-skipped"), [])

    def test_apply_is_refused_for_geometric_mode(self) -> None:
        self.assertGreaterEqual(0.3, GRID_DELTA)
        step = isotropic_step("etch", rate=0.3, duration=1.0)
        step["isotropicMethod"] = "geometric"
        ctx = self._context("apply-geometric", step)
        original_domain = ctx.domain
        before = _top(ctx.domain)

        with _v2_flags(apply=True), mock.patch("worker.process_models_v2.run_v2_step") as bridge:
            run_process_step(ctx, 1, step)

        bridge.assert_not_called()
        self.assertIs(ctx.domain, original_domain)
        self.assertLess(_top(ctx.domain) - before, -0.1)
        skipped = _events_of(ctx.run_dir, "v2-plugin-shadow-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("geometric", skipped[0]["reason"].lower())
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-shadow"), [])
        self.assertEqual(_events_of(ctx.run_dir, "v2-plugin-applied"), [])


class V2PluginImportSafetyTests(unittest.TestCase):
    def test_importing_the_step_module_does_not_import_the_bridge(self) -> None:
        """Default off must stay import-free, not just event-free."""
        code = (
            "import sys\n"
            "import worker.steps.process\n"
            "loaded = sorted(name for name in sys.modules if name in "
            "{'worker.process_models_v2', 'plugins', 'plugins.isotropic'})\n"
            "assert not loaded, loaded\n"
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class V2PluginSkipReasonTests(unittest.TestCase):
    """The skip reason names the dispatch fields without assuming a model."""

    def test_material_rate_mode_reason(self) -> None:
        step = isotropic_step("etch", rate=0.0)
        step["rateMode"] = "material"
        step["materialRates"] = {"Si": 0.2}

        reason = _v2_plugin_skip_reason(step)

        self.assertIn("material", reason.lower())
        self.assertIn("model='isotropic'", reason)

    def test_geometric_method_reason(self) -> None:
        step = isotropic_step("etch", rate=0.3)
        step["isotropicMethod"] = "geometric"

        reason = _v2_plugin_skip_reason(step)

        self.assertIn("geometric", reason.lower())

    def test_non_plugin_model_reason(self) -> None:
        step = {"id": "s", "model": "specialized", "operation": "deposit"}

        reason = _v2_plugin_skip_reason(step)

        self.assertIn("model='specialized'", reason)


if __name__ == "__main__":
    unittest.main()
