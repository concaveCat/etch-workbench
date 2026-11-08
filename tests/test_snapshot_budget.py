"""Deferred snapshots stay readable when the finished run is already over budget.

A run that reached its artifact cap through its own native states used to lose
its last deferred snapshot: materialisation re-ran
``worker.simulate.save_snapshot`` inside the run directory, and that function
refuses to write once the directory is over the cap. These tests pin the fix
(regenerate in an isolated staging directory, publish with ``os.replace``) and
pin that the configured cap (here set to the legacy 512 MiB) still applies to
new runs and to the run archive.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import runtime_limits
from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request, json_body
from tests.test_materials_milestone import project as materials_project, step as materials_step
from worker import simulate

ROOT = Path(__file__).resolve().parents[1]
CAP = 512 * 1024**2


def pad_run_directory(run_dir: Path, cap: int = CAP, extra_bytes: int = 8 * 1024**2) -> Path:
    """Grow a run directory past the cap with a sparse file (``st_size`` counts)."""
    padding = run_dir / "padding.bin"
    with padding.open("wb") as stream:
        stream.truncate(cap + extra_bytes)
    return padding


class OverCapSnapshotReadback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "512"})
        self.env.start()
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self):
        await self.life.__aexit__(None, None, None)
        self.env.stop()

    async def wait(self, run_id):
        for _ in range(600):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded acceptance timeout")

    async def execute(self, value):
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        return run_id, await self.wait(run_id)

    async def test_deferred_final_snapshot_of_an_over_cap_run_is_readable(self):
        value = materials_project(2)
        value["steps"] = [materials_step("isotropic", "etch", rate=0.2)]
        run_id, info = await self.execute(value)
        run_dir = storage.run_path(run_id)

        step_id = info["snapshots"][-1]["stepId"]
        meta_path = run_dir / f"{step_id}.meta.json"
        self.assertTrue(meta_path.is_file(), "snapshot was not deferred; the regression cannot be reproduced")
        self.assertFalse((run_dir / f"{step_id}.json").is_file())
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))["metadata"]

        # A terminal run whose own artifacts already exceed the cap: this is the
        # state run 516a55ba12b88d6ae7b17f40 was in when the snapshot 500'd.
        padding = pad_run_directory(run_dir)
        try:
            self.assertGreater(storage.directory_bytes(run_dir), CAP)

            path = snapshot_store.ensure_materialized(run_dir, step_id)
            self.assertEqual(path, run_dir / f"{step_id}.json")
            snapshot = storage.read_json(path)
            self.assertEqual(len(snapshot["points"]), metadata["pointCount"])
            self.assertEqual(len(snapshot["lines"]), metadata["lineCount"])
            self.assertFalse(meta_path.exists(), "deferred metadata must be retired after materialisation")

            # The public API serves the same document.
            response = await asgi_request(f"/api/runs/{run_id}/snapshots/{step_id}")
            self.assertEqual(response[0], 200, response[2])
            served = json_body(response)
            self.assertEqual(len(served["points"]), metadata["pointCount"])
            self.assertEqual(len(served["lines"]), metadata["lineCount"])

            # Materialisation must not leak its staging tree.
            staging_root = snapshot_store.STAGING_ROOT
            leftovers = list(staging_root.iterdir()) if staging_root.is_dir() else []
            self.assertEqual(leftovers, [])

            # A terminal over-cap run still downloads while its *packaged*
            # entries fit; the archive cap itself is unchanged.
            for item in info["snapshots"]:
                snapshot_store.ensure_materialized(run_dir, item["stepId"])
            archive = await asgi_request(f"/api/runs/{run_id}/download")
            self.assertEqual(archive[0], 200, archive[2][:200])

            # Grow a file the manifest packages (the native state) past the cap:
            # the run ZIP must still be refused with 413.
            manifest = storage.read_json(run_dir / "manifest.json")
            native = next(item["nativeState"] for item in manifest["snapshots"]
                          if isinstance(item.get("nativeState"), dict))
            with (run_dir / native["fileName"]).open("r+b") as stream:
                stream.truncate(CAP + 8 * 1024**2)
            archive = await asgi_request(f"/api/runs/{run_id}/download")
            self.assertEqual(archive[0], 413, archive[2][:200])
        finally:
            padding.unlink(missing_ok=True)

    async def test_materialisation_failure_reports_the_staging_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            step_id = "step_budget"
            (run_dir / "input.json").write_text("{}", encoding="utf-8")
            (run_dir / f"{step_id}.meta.json").write_text(
                json.dumps({"schemaVersion": 1, "deferred": True, "metadata": {
                    "stepId": step_id, "name": "budget", "stepIndex": 0, "snapshot": f"{step_id}.json"}}),
                encoding="utf-8")
            (run_dir / f"state-{step_id}.vpsd").write_bytes(b"state")
            log_path = run_dir / f".materialize-{step_id}.log"
            limit_mib = runtime_limits.artifact_limit_mib()
            log_path.write_text(f"RuntimeError: run artifacts exceeded the {limit_mib} MiB application cap\n",
                                encoding="utf-8")
            with patch.object(snapshot_store.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 1, "", "")):
                with self.assertRaises(RuntimeError) as raised:
                    snapshot_store.ensure_materialized(run_dir, step_id)
            message = str(raised.exception)
            self.assertIn(f"run artifacts exceeded the {limit_mib} MiB application cap", message)
            self.assertIn(f"does not fit the {limit_mib} MiB artifact cap", message)
            self.assertIn(str(snapshot_store.STAGING_ROOT), message)
            self.assertFalse((run_dir / f"{step_id}.json").exists())


class StagingPublish(unittest.TestCase):
    def test_publish_moves_auxiliary_outputs_without_following_staged_links(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            run_dir = base / "run"
            staging = base / "staging"
            (run_dir / "artifacts").mkdir(parents=True)
            (staging / "artifacts").mkdir(parents=True)
            (run_dir / "input.json").write_text("{}", encoding="utf-8")
            (run_dir / "state-step.vpsd").write_bytes(b"native state")
            (run_dir / "step.meta.json").write_text('{"deferred": true}', encoding="utf-8")
            (staging / "step.meta.json").symlink_to(run_dir / "step.meta.json")
            (staging / "state-step.vpsd").symlink_to(run_dir / "state-step.vpsd")
            (staging / "step.json").write_text('{"points": []}', encoding="utf-8")
            (staging / "step.vtk").write_bytes(b"vtk")
            (staging / "artifacts" / "step-domain-metadata.json").write_text('{"values": {}}', encoding="utf-8")
            (staging / "artifacts" / "step-levelsets.vtk_layer0.lvst").write_bytes(b"lvst")

            target = snapshot_store._publish(staging, run_dir, "step")

            self.assertEqual(target, run_dir / "step.json")
            self.assertEqual(target.read_text(encoding="utf-8"), '{"points": []}')
            self.assertEqual((run_dir / "step.vtk").read_bytes(), b"vtk")
            self.assertEqual((run_dir / "artifacts" / "step-domain-metadata.json").read_text(encoding="utf-8"),
                             '{"values": {}}')
            self.assertEqual((run_dir / "artifacts" / "step-levelsets.vtk_layer0.lvst").read_bytes(), b"lvst")
            # The staged read-only inputs stay links; the run's own copies are
            # never moved over themselves.
            self.assertTrue((staging / "state-step.vpsd").is_symlink())
            self.assertTrue((staging / "step.meta.json").is_symlink())
            self.assertEqual((run_dir / "state-step.vpsd").read_bytes(), b"native state")
            self.assertFalse((staging / "step.json").exists())


class ArtifactCapStillApplies(unittest.TestCase):
    def test_new_run_over_the_cap_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            pad_run_directory(run_dir)
            # Explicit legacy cap: this is the 512 MiB behavior we must keep.
            with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "512"}):
                with self.assertRaises(RuntimeError) as worker_raised:
                    simulate.check_artifact_budget(run_dir)
                self.assertIn("512 MiB application cap", str(worker_raised.exception))
                with self.assertRaises(OSError) as supervisor_raised:
                    storage.check_artifact_budget(run_dir)
                self.assertIn("512 MiB application cap", str(supervisor_raised.exception))

    def test_over_512_mib_is_accepted_by_the_default_4_gib_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with (run_dir / "padding.bin").open("wb") as stream:
                stream.truncate(640 * 1024**2)
            # Default cap without touching 4 GiB of real data.
            simulate.check_artifact_budget(run_dir)
            storage.check_artifact_budget(run_dir)
            with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "512"}):
                with self.assertRaises(RuntimeError) as worker_raised:
                    simulate.check_artifact_budget(run_dir)
                self.assertIn("512 MiB application cap", str(worker_raised.exception))
                with self.assertRaises(OSError) as supervisor_raised:
                    storage.check_artifact_budget(run_dir)
                self.assertIn("512 MiB application cap", str(supervisor_raised.exception))

    def test_run_directory_at_exactly_the_cap_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with (run_dir / "padding.bin").open("wb") as stream:
                stream.truncate(CAP)
            with patch.dict(os.environ, {"WORKBENCH_MAX_ARTIFACT_MIB": "512"}):
                self.assertEqual(storage.directory_bytes(run_dir), CAP)
                simulate.check_artifact_budget(run_dir)
                storage.check_artifact_budget(run_dir)


if __name__ == "__main__":
    unittest.main()
