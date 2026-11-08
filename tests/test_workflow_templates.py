"""P1 templates, scalar variables and flow diff."""

from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_adoption import project  # noqa: E402


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def test_append_template_preserves_three_steps_and_adds_two(self):
        suffix = uuid.uuid4().hex[:6]
        template_id = f"tpl_append_{suffix}"
        base = project()
        base_step = base["steps"][0]
        base["steps"] = [
            {**base_step, "id": f"base_step_{index:02d}", "name": f"base {index}"}
            for index in range(1, 4)
        ]
        original = await asgi_request(
            "/api/v1/workflow/flows/from-project", method="POST",
            payload={"project": base},
        )
        self.assertEqual(original[0], 201, original[2])
        original_body = json_body(original)
        self.assertEqual(original_body["stepCount"], 3)

        template_steps = [
            {**base_step, "id": f"tpl_source_{index:02d}", "name": f"insert {index}",
             "duration": "${duration}", "rateMagnitude": 0.0}
            for index in range(1, 3)
        ]
        created = await asgi_request(
            "/api/v1/workflow/templates", method="POST",
            payload={
                "schemaVersion": 1, "templateId": template_id, "name": "two step insert",
                "description": "", "steps": template_steps,
                "variables": [{"name": "duration", "value": 0.1, "unit": "s"}],
            },
        )
        self.assertEqual(created[0], 201, created[2])

        appended = await asgi_request(
            "/api/v1/workflow/flows/from-template", method="POST",
            payload={
                "project": base, "templateId": template_id,
                "flowId": original_body["flowId"], "variables": {"duration": 0.2},
                "mode": "append",
            },
        )
        self.assertEqual(appended[0], 201, appended[2])
        body = json_body(appended)
        self.assertEqual(body["stepCount"], 5)
        authored = [step["id"] for step in body["authoredSteps"]]
        self.assertEqual(authored[:3], ["base_step_01", "base_step_02", "base_step_03"])
        self.assertEqual(len(set(authored)), 5)
        self.assertTrue(all(step_id.startswith("tpl_") for step_id in authored[3:]))
        self.assertEqual([step["duration"] for step in body["authoredSteps"][3:]], [0.2, 0.2])
        self.assertEqual(
            [entry["authoredStepId"] for entry in body["compile"]["sourceMap"]],
            authored,
        )
        diff = await asgi_request(
            f"/api/v1/workflow/compiles/{original_body['compileId']}/diff/{body['compileId']}"
        )
        self.assertEqual(diff[0], 200, diff[2])
        diff_body = json_body(diff)
        self.assertEqual(len(diff_body["addedStepIds"]), 2)
        self.assertEqual(diff_body["removedStepIds"], [])
        self.assertEqual(diff_body["changedStepIds"], [])
        self.assertEqual(diff_body["movedStepIds"], [])

    async def test_template_variable_substitution_and_diff(self):
        suffix = uuid.uuid4().hex[:6]
        template_id = f"tpl_{suffix}"
        base_project = project()
        steps = [dict(item) for item in base_project["steps"]]
        steps[0]["duration"] = "${duration}"
        created = await asgi_request(
            "/api/v1/workflow/templates",
            method="POST",
            payload={
                "schemaVersion": 1,
                "templateId": template_id,
                "name": "variable template",
                "description": "",
                "steps": steps,
                "variables": [{"name": "duration", "value": 0.1, "unit": "s"}],
            },
        )
        self.assertEqual(created[0], 201, created[2])
        compiled = await asgi_request(
            "/api/v1/workflow/flows/from-template",
            method="POST",
            payload={"project": base_project, "templateId": template_id, "variables": {"duration": 0.2}},
        )
        self.assertEqual(compiled[0], 201, compiled[2])
        body = json_body(compiled)
        self.assertAlmostEqual(body["compile"]["project"]["steps"][0]["duration"], 0.2)
        diff = await asgi_request(
            f"/api/v1/workflow/compiles/{body['compileId']}/diff/{body['compileId']}"
        )
        self.assertEqual(diff[0], 200, diff[2])
        self.assertEqual(json_body(diff)["changedStepIds"], [])


if __name__ == "__main__":
    unittest.main()
