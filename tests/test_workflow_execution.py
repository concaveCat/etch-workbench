"""P0-2 real execution: Segment -> supervisor -> terminal segment state."""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from backend.supervisor import ActiveRunError  # noqa: E402
from backend.workflow import execution as execution_module  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from backend.workflow.routes import AttemptV1, RoutePlanV1, RouteV1, SegmentV1  # noqa: E402
from backend.workflow.hashing import full_project_hash  # noqa: E402
from tests.workflow_fixtures import binding_plan_2d, compiled_2d_zero, compiled_2d_zero_two  # noqa: E402


class FakeSupervisor:
    """One-slot supervisor that can be cancelled deterministically, no GPU."""

    def __init__(self, *, stop_immediately: bool = True):
        self.runs: dict[str, str] = {}
        self.active: str | None = None
        self.cancelled: list[str] = []
        self.stop_immediately = stop_immediately

    def start(self, kind: str, project: dict, **_: object) -> dict:
        if self.active is not None:
            raise ActiveRunError(self.active)
        run_id = uuid.uuid4().hex[:24]
        self.runs[run_id] = "running"
        self.active = run_id
        return {"runId": run_id, "status": "running"}

    def cancel(self, run_id: str) -> dict:
        self.cancelled.append(run_id)
        if self.runs.get(run_id) in {"pending", "running"}:
            self.runs[run_id] = "cancelling"
        return {"runId": run_id, "status": self.runs.get(run_id, "unknown")}

    def get_run(self, run_id: str, *, cursor: int = 0) -> dict:
        status = self.runs.get(run_id, "interrupted")
        if status == "cancelling" and self.stop_immediately:
            # Like a real worker: the next poll observes the terminal state.
            self.runs[run_id] = "cancelled"
            if self.active == run_id:
                self.active = None
            status = "cancelled"
        return {"runId": run_id, "status": status}

    def _status(self, run_id: str) -> str:
        return self.runs.get(run_id, "unknown")


class WorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.compiled = compiled_2d_zero()
        registered = await asgi_request(
            "/api/v1/workflow/compiles",
            method="POST",
            payload=self.compiled.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(registered[0], 201, registered[2])
        self.suffix = uuid.uuid4().hex[:6]
        placeholder_run = "0" * 24
        plan = binding_plan_2d(
            self.compiled,
            suffix=self.suffix,
            run_id=placeholder_run,
            state_hash="0" * 64,
        )
        payload = plan.model_dump(mode="json", exclude_none=True)
        payload["attempts"][0]["status"] = "planned"
        payload["segments"][0]["status"] = "planned"
        created = await asgi_request("/api/v1/workflow/routes", method="POST", payload=payload)
        self.assertEqual(created[0], 201, created[2])
        self.segment_id = plan.segments[0].segmentId

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def test_execute_segment_updates_status_and_checkpoint(self):
        started = await asgi_request(
            f"/api/v1/workflow/segments/{self.segment_id}/execute",
            method="POST",
            payload={},
        )
        self.assertEqual(started[0], 200, started[2])
        body = json_body(started)
        self.assertEqual(body["segment"]["status"], "running")
        self.assertRegex(body["runId"], r"^[a-f0-9]{24}$")

        deadline = asyncio.get_running_loop().time() + 30.0
        segment = None
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/v1/workflow/segments/{self.segment_id}")
            self.assertEqual(response[0], 200, response[2])
            segment = json_body(response)
            if segment["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            await asyncio.sleep(0.2)
        self.assertIsNotNone(segment)
        self.assertEqual(segment["status"], "succeeded", segment)
        self.assertEqual(segment["runId"], body["runId"])
        self.assertIsNotNone(segment["endCheckpointRef"])
        self.assertEqual(segment["endCheckpointRef"]["stepId"], segment["throughStepId"])
        self.assertRegex(segment["segmentProjectHash"], r"^[a-f0-9]{64}$")


    async def test_two_segment_continuation_resolves_checkpoint(self):
        compiled = compiled_2d_zero_two()
        registered = await asgi_request(
            "/api/v1/workflow/compiles",
            method="POST",
            payload=compiled.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(registered[0], 201, registered[2])
        suffix = uuid.uuid4().hex[:6]
        step_ids = [step.id for step in compiled.project.steps]
        attempt = AttemptV1(
            attemptId=f"twoA{suffix}", routeId=f"twoR{suffix}",
            compileId=compiled.compileRecord.compileId, submissionKey=f"twoS{suffix}",
            startAfterStepId=None, throughStepId=step_ids[-1],
            seedPolicy="shared-run-seed", runSeed=compiled.project.solver.random.seed,
            flowDigest=compiled.compileRecord.fullProjectHash, status="planned",
        )
        s1 = SegmentV1(
            segmentId=f"twoS{suffix}1", attemptId=attempt.attemptId,
            sourceCompileId=compiled.compileRecord.compileId,
            segmentProjectHash=full_project_hash(compiled.project),
            startAfterStepId=None, throughStepId=step_ids[0],
            startCheckpointRef=None, endCheckpointRef=None,
            runId=f"twoR{suffix}1", provenanceInputHash="0" * 64,
            flowDigest=compiled.compileRecord.fullProjectHash,
            executionHash="0" * 64, status="planned",
        )
        s2 = SegmentV1(
            segmentId=f"twoS{suffix}2", attemptId=attempt.attemptId,
            sourceCompileId=compiled.compileRecord.compileId,
            segmentProjectHash=full_project_hash(compiled.project),
            startAfterStepId=step_ids[0], throughStepId=step_ids[1],
            startCheckpointRef=None, endCheckpointRef=None,
            runId=f"twoR{suffix}2", provenanceInputHash="0" * 64,
            flowDigest=compiled.compileRecord.fullProjectHash,
            executionHash="0" * 64, status="planned",
        )
        plan = RoutePlanV1(
            route=RouteV1(routeId=attempt.routeId, flowId="flow_zero_2d_02", name="two segment", headAttemptId=attempt.attemptId),
            stepOrder=step_ids, attempts=[attempt], segments=[s1, s2], reuse=[],
        )
        created = await asgi_request("/api/v1/workflow/routes", method="POST", payload=plan.model_dump(mode="json", exclude_none=True))
        self.assertEqual(created[0], 201, created[2])

        async def run_and_wait(segment_id: str):
            started = await asgi_request(f"/api/v1/workflow/segments/{segment_id}/execute", method="POST", payload={})
            self.assertEqual(started[0], 200, started[2])
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                response = await asgi_request(f"/api/v1/workflow/segments/{segment_id}")
                self.assertEqual(response[0], 200, response[2])
                body = json_body(response)
                if body["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    return body
                await asyncio.sleep(0.2)
            self.fail("segment did not finish")

        first = await run_and_wait(s1.segmentId)
        self.assertEqual(first["status"], "succeeded", first)
        second = await asgi_request(f"/api/v1/workflow/segments/{s2.segmentId}")
        self.assertEqual(second[0], 200, second[2])
        self.assertIsNotNone(json_body(second)["startCheckpointRef"])
        final = await run_and_wait(s2.segmentId)
        self.assertEqual(final["status"], "succeeded", final)

    async def _independent_route(self, tag: str) -> str:
        """One planned Segment that starts from the initial state.

        Two such routes are the user's "another Segment": neither needs the
        other's checkpoint, so B can run while A is still active.
        """

        compiled = self.compiled
        step_ids = [step.id for step in compiled.project.steps]
        suffix = f"{self.suffix}{tag}"
        attempt = AttemptV1(
            attemptId=f"preA{suffix}", routeId=f"preR{suffix}",
            compileId=compiled.compileRecord.compileId, submissionKey=f"preS{suffix}",
            startAfterStepId=None, throughStepId=step_ids[-1],
            seedPolicy="shared-run-seed", runSeed=compiled.project.solver.random.seed,
            flowDigest=compiled.compileRecord.fullProjectHash, status="planned",
        )
        segment = SegmentV1(
            segmentId=f"preG{suffix}", attemptId=attempt.attemptId,
            sourceCompileId=compiled.compileRecord.compileId,
            segmentProjectHash=full_project_hash(compiled.project),
            startAfterStepId=None, throughStepId=step_ids[-1],
            startCheckpointRef=None, endCheckpointRef=None,
            runId=f"preRun{suffix}", provenanceInputHash="0" * 64,
            flowDigest=compiled.compileRecord.fullProjectHash,
            executionHash="0" * 64, status="planned",
        )
        plan = RoutePlanV1(
            route=RouteV1(routeId=attempt.routeId, flowId=compiled.compileRecord.flowId,
                          name=f"preempt {tag}", headAttemptId=attempt.attemptId),
            stepOrder=step_ids, attempts=[attempt], segments=[segment], reuse=[],
        )
        created = await asgi_request("/api/v1/workflow/routes", method="POST",
                                     payload=plan.model_dump(mode="json", exclude_none=True))
        self.assertEqual(created[0], 201, created[2])
        return segment.segmentId

    async def _wait_segment(self, segment_id: str, timeout: float = 10.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        body: dict = {}
        while loop.time() < deadline:
            response = await asgi_request(f"/api/v1/workflow/segments/{segment_id}")
            self.assertEqual(response[0], 200, response[2])
            body = json_body(response)
            if body["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return body
            await asyncio.sleep(0.05)
        self.fail(f"segment {segment_id} did not reach a terminal state: {body}")

    async def test_execute_preempts_the_active_run(self):
        first = await self._independent_route("a")
        second = await self._independent_route("b")
        fake = FakeSupervisor()
        original = app.state.supervisor
        app.state.supervisor = fake
        try:
            started_a = await asgi_request(
                f"/api/v1/workflow/segments/{first}/execute", method="POST", payload={})
            self.assertEqual(started_a[0], 200, started_a[2])
            run_a = json_body(started_a)["runId"]

            # B starts while A holds the only slot: A is cancelled, B runs.
            started_b = await asgi_request(
                f"/api/v1/workflow/segments/{second}/execute", method="POST", payload={})
            self.assertEqual(started_b[0], 200, started_b[2])
            run_b = json_body(started_b)["runId"]
            self.assertIn(run_a, fake.cancelled)
            self.assertNotEqual(run_a, run_b)

            old = await self._wait_segment(first)
            self.assertIn(old["status"], {"cancelled", "interrupted"}, old)
            self.assertEqual(old["runId"], run_a)
            current = json_body(await asgi_request(f"/api/v1/workflow/segments/{second}"))
            self.assertEqual(current["status"], "running", current)
        finally:
            # Stop B's monitor thread and restore the real supervisor.
            if fake.active:
                fake.runs[fake.active] = "cancelled"
                fake.active = None
            app.state.supervisor = original

    async def test_execute_reports_a_run_that_will_not_stop(self):
        first = await self._independent_route("c")
        second = await self._independent_route("d")
        fake = FakeSupervisor(stop_immediately=False)
        original = app.state.supervisor
        app.state.supervisor = fake
        try:
            started = await asgi_request(
                f"/api/v1/workflow/segments/{first}/execute", method="POST", payload={})
            self.assertEqual(started[0], 200, started[2])
            run_a = json_body(started)["runId"]
            with patch.object(execution_module, "CANCEL_TIMEOUT_SECONDS", 0.3):
                blocked = await asgi_request(
                    f"/api/v1/workflow/segments/{second}/execute", method="POST", payload={})
            self.assertEqual(blocked[0], 409, blocked[2])
            self.assertEqual(json_body(blocked)["detail"], f"上一个运行停止超时：{run_a}")
            # The timed-out attempt never marks the new Segment as running.
            still_planned = json_body(await asgi_request(
                f"/api/v1/workflow/segments/{second}"))
            self.assertEqual(still_planned["status"], "planned", still_planned)
        finally:
            fake.runs[run_a] = "cancelled"
            fake.active = None
            app.state.supervisor = original

    async def test_execute_rejects_non_planned_segment(self):
        started = await asgi_request(
            f"/api/v1/workflow/segments/{self.segment_id}/execute",
            method="POST",
            payload={},
        )
        self.assertEqual(started[0], 200, started[2])
        again = await asgi_request(
            f"/api/v1/workflow/segments/{self.segment_id}/execute",
            method="POST",
            payload={},
        )
        self.assertEqual(again[0], 409, again[2])


if __name__ == "__main__":
    unittest.main()
