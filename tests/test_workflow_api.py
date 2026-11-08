"""P0-2 workflow API: verified compiles, route persistence, branches, idempotency."""

from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.workflow_fixtures import (  # noqa: E402
    base_plan_from_compiled,
    compiled_base,
    compiled_branch,
    plan_from_compiled,
)


class WorkflowApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.base = compiled_base()
        self.branch = compiled_branch()
        for compiled in (self.base, self.branch):
            response = await asgi_request(
                "/api/v1/workflow/compiles",
                method="POST",
                payload=compiled.model_dump(mode="json", exclude_none=True),
            )
            self.assertEqual(response[0], 201, response[2])

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    def suffix(self) -> str:
        return uuid.uuid4().hex[:6]

    async def _create_base(self, suffix: str):
        plan = base_plan_from_compiled(self.base, suffix=suffix)
        response = await asgi_request(
            "/api/v1/workflow/routes",
            method="POST",
            payload=plan.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(response[0], 201, response[2])
        return plan

    async def test_create_read_and_list_route(self):
        suffix = self.suffix()
        plan = await self._create_base(suffix)
        fetched = await asgi_request(f"/api/v1/workflow/routes/{plan.route.routeId}")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched)["plan"]["route"]["routeId"], plan.route.routeId)

        listed = await asgi_request("/api/v1/workflow/routes")
        self.assertEqual(listed[0], 200, listed[2])
        rows = json_body(listed)["routes"]
        self.assertTrue(any(row["routeId"] == plan.route.routeId for row in rows))

    async def test_append_branch_is_idempotent_and_materializes_prefix(self):
        suffix = self.suffix()
        base = await self._create_base(suffix)
        full = plan_from_compiled(self.base, self.branch, suffix=suffix)
        body = {
            "attempt": full.attempts[1].model_dump(mode="json", exclude_none=True),
            "segments": [full.segments[2].model_dump(mode="json", exclude_none=True)],
            "reuse": [full.reuse[0].model_dump(mode="json", exclude_none=True)],
        }
        first = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(first[0], 200, first[2])
        first_plan = json_body(first)["plan"]
        self.assertEqual(first_plan["route"]["headAttemptId"], full.attempts[1].attemptId)
        self.assertEqual(len(first_plan["attempts"]), 2)

        second = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(second[0], 200, second[2])
        second_plan = json_body(second)["plan"]
        self.assertEqual(len(second_plan["attempts"]), 2)
        self.assertEqual(len(second_plan["segments"]), 3)

        materialized = await asgi_request(
            f"/api/v1/workflow/routes/{base.route.routeId}/attempts/{full.attempts[1].attemptId}/materialize"
        )
        self.assertEqual(materialized[0], 200, materialized[2])
        refs = json_body(materialized)["segments"]
        self.assertEqual([item["reused"] for item in refs], [True, False])
        self.assertEqual([item["segmentId"] for item in refs], [full.segments[0].segmentId, full.segments[2].segmentId])

    async def test_same_submission_key_with_changed_content_returns_409(self):
        suffix = self.suffix()
        base = await self._create_base(suffix)
        full = plan_from_compiled(self.base, self.branch, suffix=suffix)
        body = {
            "attempt": full.attempts[1].model_dump(mode="json", exclude_none=True),
            "segments": [full.segments[2].model_dump(mode="json", exclude_none=True)],
            "reuse": [full.reuse[0].model_dump(mode="json", exclude_none=True)],
        }
        first = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(first[0], 200, first[2])
        body["attempt"]["changedStepIds"] = [full.stepOrder[4]]
        conflict = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(conflict[0], 409, conflict[2])

    async def test_same_submission_key_for_another_attempt_returns_409(self):
        suffix = self.suffix()
        base = await self._create_base(suffix)
        full = plan_from_compiled(self.base, self.branch, suffix=suffix)
        body = {
            "attempt": full.attempts[1].model_dump(mode="json", exclude_none=True),
            "segments": [full.segments[2].model_dump(mode="json", exclude_none=True)],
            "reuse": [full.reuse[0].model_dump(mode="json", exclude_none=True)],
        }
        first = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(first[0], 200, first[2])
        body["attempt"]["attemptId"] = f"A{suffix}9"
        conflict = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(conflict[0], 409, conflict[2])

    async def test_cancel_attempt_and_segment(self):
        suffix = self.suffix()
        plan = base_plan_from_compiled(self.base, suffix=suffix)
        payload = plan.model_dump(mode="json", exclude_none=True)
        payload["attempts"][0]["status"] = "planned"
        payload["segments"][0]["status"] = "planned"
        payload["segments"][1]["status"] = "planned"
        created = await asgi_request("/api/v1/workflow/routes", method="POST", payload=payload)
        self.assertEqual(created[0], 201, created[2])
        attempt_id = payload["attempts"][0]["attemptId"]
        segment_id = payload["segments"][0]["segmentId"]

        cancelled = await asgi_request(f"/api/v1/workflow/attempts/{attempt_id}/cancel", method="POST")
        self.assertEqual(cancelled[0], 200, cancelled[2])
        self.assertEqual(json_body(cancelled)["status"], "cancelled")
        again = await asgi_request(f"/api/v1/workflow/attempts/{attempt_id}/cancel", method="POST")
        self.assertEqual(again[0], 409, again[2])

        segment_cancelled = await asgi_request(f"/api/v1/workflow/segments/{segment_id}/cancel", method="POST")
        self.assertEqual(segment_cancelled[0], 200, segment_cancelled[2])
        self.assertEqual(json_body(segment_cancelled)["status"], "cancelled")

    async def test_duplicate_route_returns_409(self):
        suffix = self.suffix()
        plan = base_plan_from_compiled(self.base, suffix=suffix)
        payload = plan.model_dump(mode="json", exclude_none=True)
        first = await asgi_request("/api/v1/workflow/routes", method="POST", payload=payload)
        self.assertEqual(first[0], 201, first[2])
        second = await asgi_request("/api/v1/workflow/routes", method="POST", payload=payload)
        self.assertEqual(second[0], 409, second[2])

    async def test_invalid_branch_returns_422(self):
        suffix = self.suffix()
        base = await self._create_base(suffix)
        full = plan_from_compiled(self.base, self.branch, suffix=suffix)
        body = {
            "attempt": full.attempts[1].model_dump(mode="json", exclude_none=True),
            "segments": [full.segments[2].model_dump(mode="json", exclude_none=True)],
            "reuse": [full.reuse[0].model_dump(mode="json", exclude_none=True)],
        }
        body["segments"][0]["startAfterStepId"] = full.stepOrder[4]
        response = await asgi_request(f"/api/v1/workflow/routes/{base.route.routeId}/attempts", method="POST", payload=body)
        self.assertEqual(response[0], 422, response[2])

    async def test_missing_route_returns_404(self):
        response = await asgi_request("/api/v1/workflow/routes/DOES_NOT_EXIST")
        self.assertEqual(response[0], 404, response[2])


if __name__ == "__main__":
    unittest.main()
