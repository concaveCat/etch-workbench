"""End-to-end capability validation for the validation-only v2 plugin apply.

``VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY=1`` makes the worker use the v2 plugin
domain as the result of an eligible uniform level-set step after the shadow
comparison matched. Unlike the unit-level tests in
``tests/test_worker_v2_isotropic_shadow.py`` these tests drive the real API:

* a 2D three-step flow (deposit / etch / deposit) is run once with both v2
  flags unset and once with the apply flag set; every step snapshot, every
  native state ID and the run history must be identical, and the apply run
  must record one ``v2-plugin-applied`` event per eligible step;
* that apply run is replayed through ``/api/runs/{id}/replay-project`` +
  ``/api/runs/replay`` with the apply flag still set, so the replay exercises
  the same v2 path. Its final snapshot and native state must match both the
  apply run and the baseline;
* a smaller 3D two-step flow repeats the baseline/apply comparison without a
  replay (one 2D replay is enough for this capability check).

Everything runs against the in-process ASGI app and the bounded CPU worker.
The tests assume an isolated ``VIENNAPS_WORKBENCH_DATA_DIR`` and never touch
production defaults: the apply flag is only set inside a test.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from backend import snapshots as snapshot_store
from backend import storage
from backend.main import app
from tests.test_api_integration import asgi_request, json_body
from tests.test_isotropic_dispatch import iso_step, project

SHADOW_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_SHADOW"
APPLY_ENV = "VIENNAPS_WORKBENCH_V2_PLUGIN_APPLY"

# ``outputs`` are run-directory-relative artifacts and ``runId`` names the
# run; everything else is the geometry/readback contract the apply mode must
# preserve.
SNAPSHOT_IGNORED_KEYS = frozenset({"runId", "outputs"})


@contextlib.contextmanager
def v2_flags(*, apply: bool = False):
    """Set or clear both v2 hook flags for the duration of the block.

    The worker is a bounded child process and inherits ``os.environ`` at spawn
    time, so the patch must wrap the whole submit/wait, not just the request.
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in (SHADOW_ENV, APPLY_ENV):
            os.environ.pop(name, None)
        if apply:
            os.environ[APPLY_ENV] = "1"
        yield


def flow_2d() -> dict[str, Any]:
    """Three uniform level-set isotropic steps; explicit levelSet keeps them eligible.

    The second deposit deliberately grows SiO2 again. The worker's old deposit
    path calls ``Domain.duplicateTopLevelSet`` even when the material already
    exists, producing duplicate level-set material IDs (``[10, 30, 30]``).
    v2 ``MaterialState.order`` is per level-set index and must allow that; the
    capability test pins the repeated-material case rather than avoiding it.
    """
    value = project(2)
    value["name"] = "v2 各向同性 apply 2D 多步"
    value["steps"] = [
        iso_step(
            "deposit",
            method="levelSet",
            rate=0.2,
            duration=1.0,
            step_id="step_v2_deposit_1",
            name="沉积一",
        ),
        iso_step(
            "etch",
            method="levelSet",
            rate=0.1,
            duration=1.0,
            step_id="step_v2_etch_1",
            name="刻蚀一",
        ),
        iso_step(
            "deposit",
            method="levelSet",
            rate=0.15,
            duration=1.0,
            step_id="step_v2_deposit_2",
            name="沉积二",
            depositMaterial="SiO2",
        ),
    ]
    return value


def flow_3d() -> dict[str, Any]:
    value = project(3)
    value["name"] = "v2 各向同性 apply 3D 两步"
    value["steps"] = [
        iso_step(
            "deposit",
            method="levelSet",
            rate=0.2,
            duration=1.0,
            step_id="step_v2_3d_deposit",
            name="三维沉积",
        ),
        iso_step(
            "etch",
            method="levelSet",
            rate=0.1,
            duration=1.0,
            step_id="step_v2_3d_etch",
            name="三维刻蚀",
        ),
    ]
    return value


def v2_events(run_dir: Path, event_type: str) -> list[dict[str, Any]]:
    path = run_dir / "events.ndjson"
    if not path.is_file():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [record for record in records if record.get("type") == event_type]


class V2IsotropicApplyCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Preserve any operator-provided flags so this test neither depends on
        # nor leaks into the process environment.
        self._saved_flags = {name: os.environ.get(name) for name in (SHADOW_ENV, APPLY_ENV)}
        self._created_runs: list[str] = []
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)
        # Only this test's run directories are removed; the isolated data
        # directory itself belongs to the caller.
        for run_id in self._created_runs:
            shutil.rmtree(storage.run_path(run_id), ignore_errors=True)
        for name, value in self._saved_flags.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    async def wait(self, run_id: str) -> dict[str, Any]:
        for _ in range(1200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
                self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
                return info
            await asyncio.sleep(0.05)
        self.fail("bounded native worker exceeded the acceptance timeout")

    async def execute(self, value: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = await asgi_request("/api/runs", method="POST", payload={"kind": "simulate", "project": value})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        self._created_runs.append(run_id)
        return run_id, await self.wait(run_id)

    def snapshot(self, run_id: str, step_id: str) -> dict[str, Any]:
        path = snapshot_store.ensure_materialized(storage.run_path(run_id), step_id)
        return storage.read_json(path)

    def assert_snapshot_payload_equal(self, expected: dict[str, Any], actual: dict[str, Any], label: str) -> None:
        expected = {key: value for key, value in expected.items() if key not in SNAPSHOT_IGNORED_KEYS}
        actual = {key: value for key, value in actual.items() if key not in SNAPSHOT_IGNORED_KEYS}
        self.assertTrue(expected.get("points"), f"{label}: snapshot has no points")
        self.assertTrue(actual.get("points"), f"{label}: snapshot has no points")
        self.assertEqual(sorted(expected), sorted(actual), f"{label}: snapshot keys differ")
        for key in sorted(expected):
            with self.subTest(label=label, field=key):
                self.assertEqual(expected[key], actual[key], f"{label}: field {key!r} differs")

    def snapshot_step_ids(self, info: dict[str, Any], steps: list[dict[str, Any]]) -> list[str]:
        step_ids = [step["id"] for step in steps]
        rows = [row["stepId"] for row in info["snapshots"]]
        self.assertEqual(sorted(rows), sorted(["initial", *step_ids]), rows)
        return ["initial", *step_ids]

    def native_state_id(self, info: dict[str, Any], step_id: str) -> str:
        row = next((item for item in info["snapshots"] if item.get("stepId") == step_id), None)
        self.assertIsNotNone(row, f"step {step_id!r} has no snapshot row")
        state = row.get("nativeState")
        self.assertIsInstance(state, dict, f"step {step_id!r} recorded no native domain state")
        state_id = state.get("stateId")
        self.assertIsInstance(state_id, str, state)
        self.assertEqual(len(state_id), 64, state_id)
        return state_id

    def assert_snapshots_match(
        self, first_run: str, first_info: dict, second_run: str, second_info: dict, steps: list[dict[str, Any]]
    ) -> None:
        for step_id in self.snapshot_step_ids(first_info, steps):
            with self.subTest(step=step_id):
                self.assert_snapshot_payload_equal(
                    self.snapshot(first_run, step_id),
                    self.snapshot(second_run, step_id),
                    f"{first_run} vs {second_run} / {step_id}",
                )

    def assert_native_states_match(self, first_info: dict, second_info: dict, steps: list[dict[str, Any]]) -> None:
        for step_id in self.snapshot_step_ids(first_info, steps):
            with self.subTest(step=step_id):
                self.assertEqual(
                    self.native_state_id(first_info, step_id),
                    self.native_state_id(second_info, step_id),
                    f"native state {step_id!r} differs",
                )

    async def test_2d_multi_step_apply_matches_baseline_and_replays(self) -> None:
        value = flow_2d()
        step_ids = [step["id"] for step in value["steps"]]

        with v2_flags():
            baseline_id, baseline_info = await self.execute(copy.deepcopy(value))
        with v2_flags(apply=True):
            apply_id, apply_info = await self.execute(copy.deepcopy(value))

        self.snapshot_step_ids(baseline_info, value["steps"])
        self.snapshot_step_ids(apply_info, value["steps"])

        # The default path stays v2-free; the apply run must validate and apply
        # every eligible step exactly once.
        self.assertEqual(v2_events(storage.run_path(baseline_id), "v2-plugin-applied"), [])
        self.assertEqual(v2_events(storage.run_path(baseline_id), "v2-plugin-shadow"), [])
        applied = v2_events(storage.run_path(apply_id), "v2-plugin-applied")
        self.assertEqual([record["stepId"] for record in applied], step_ids, applied)
        for record in applied:
            # Apply implies the shadow comparison, and only a matched v2 domain
            # is ever applied: the recorded digests are the validated pair.
            self.assertEqual(record["oldDigest"], record["newDigest"], record["stepId"])
            self.assertEqual(record["v2Plan"]["process_id"], "isotropic")
            self.assertEqual(record["v2Plan"]["engine_id"], "viennaps")
        shadows = v2_events(storage.run_path(apply_id), "v2-plugin-shadow")
        self.assertEqual([record["stepId"] for record in shadows], step_ids, shadows)
        for record in shadows:
            self.assertIs(record["matched"], True, record["reasons"])
            self.assertEqual(record["oldMaterialMap"], record["newMaterialMap"], record["stepId"])

        self.assert_snapshots_match(baseline_id, baseline_info, apply_id, apply_info, value["steps"])
        self.assert_native_states_match(baseline_info, apply_info, value["steps"])

        # Replay with the apply flag still set: the replay must take the same
        # v2 path, and its final snapshot/state must match both runs.
        with v2_flags(apply=True):
            replay_response = await asgi_request(f"/api/runs/{apply_id}/replay-project")
            self.assertEqual(replay_response[0], 200, replay_response[2])
            replay_document = json_body(replay_response)
            submitted = await asgi_request("/api/runs/replay", method="POST", payload=replay_document)
            self.assertEqual(submitted[0], 202, submitted[2])
            replay_id = json_body(submitted)["runId"]
            self._created_runs.append(replay_id)
            replay_info = await self.wait(replay_id)

        replay_applied = v2_events(storage.run_path(replay_id), "v2-plugin-applied")
        self.assertEqual([record["stepId"] for record in replay_applied], step_ids, replay_applied)

        final_step = step_ids[-1]
        self.assert_snapshot_payload_equal(
            self.snapshot(apply_id, final_step),
            self.snapshot(replay_id, final_step),
            "apply vs replay final snapshot",
        )
        self.assert_snapshot_payload_equal(
            self.snapshot(baseline_id, final_step),
            self.snapshot(replay_id, final_step),
            "baseline vs replay final snapshot",
        )
        self.assertEqual(
            self.native_state_id(apply_info, final_step),
            self.native_state_id(replay_info, final_step),
            "replay final native state differs from the apply run",
        )
        self.assertEqual(
            self.native_state_id(baseline_info, final_step),
            self.native_state_id(replay_info, final_step),
            "replay final native state differs from the baseline run",
        )

    async def test_3d_two_step_apply_matches_baseline(self) -> None:
        value = flow_3d()

        with v2_flags():
            baseline_id, baseline_info = await self.execute(copy.deepcopy(value))
        with v2_flags(apply=True):
            apply_id, apply_info = await self.execute(copy.deepcopy(value))

        step_ids = [step["id"] for step in value["steps"]]
        self.assertEqual(
            [record["stepId"] for record in v2_events(storage.run_path(apply_id), "v2-plugin-applied")],
            step_ids,
        )
        self.assertEqual(v2_events(storage.run_path(baseline_id), "v2-plugin-applied"), [])

        self.assert_snapshots_match(baseline_id, baseline_info, apply_id, apply_info, value["steps"])
        self.assert_native_states_match(baseline_info, apply_info, value["steps"])


if __name__ == "__main__":
    unittest.main()
