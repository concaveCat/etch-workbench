"""P1 persistent manual experiment/variant/replicate skeleton."""

from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_adoption import project  # noqa: E402


class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        suffix = uuid.uuid4().hex[:6]
        self.metric_id = f"metric_exp_{suffix}"
        self.recipe_id = f"recipe_exp_{suffix}"
        for path, payload in (
            ("/api/v1/workflow/metrics", {"schemaVersion": 1, "metricId": self.metric_id, "name": "exp", "unit": "um"}),
            ("/api/v1/workflow/metrology-recipes", {
                "schemaVersion": 1, "recipeId": self.recipe_id, "version": 1, "metricId": self.metric_id,
                "name": "exp", "dimension": 2, "method": "etch_depth",
                "parameters": {"kind": "etch_depth", "x": 0, "referenceHeight": 0}, "material": "Si",
            }),
        ):
            response = await asgi_request(path, method="POST", payload=payload)
            self.assertIn(response[0], (201, 409), response[2])

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def test_run_ten_variants_and_verify_replicate_seeds(self):
        import asyncio
        from backend import storage

        p = project()
        p["steps"][0].update({"model": "singleParticle", "rateMagnitude": 0.001, "stickingProbability": 0.1, "sourceExponent": 1.0})
        p["solver"]["raysPerPoint"] = 16
        variants = [{"parameters": {"step_etch_01": {"duration": 0.01}}, "replicates": 2 if index == 0 else 1, "seed": 200 + index} for index in range(10)]
        response = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={"name": "run ten", "project": p, "flowId": "flow_exp_run", "recipeId": self.recipe_id, "recipeVersion": 1, "variants": variants},
        )
        self.assertEqual(response[0], 201, response[2])
        document = json_body(response)
        started = await asgi_request(f"/api/v1/workflow/experiments/{document['experimentId']}/run", method="POST", payload={})
        self.assertEqual(started[0], 200, started[2])
        deadline = asyncio.get_running_loop().time() + 180
        final = None
        while asyncio.get_running_loop().time() < deadline:
            fetched = await asgi_request(f"/api/v1/workflow/experiments/{document['experimentId']}")
            final = json_body(fetched)
            if final["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.5)
        self.assertIsNotNone(final)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(len(final["variants"]), 10)
        self.assertEqual(len(final["variants"][0]["replicates"]), 2)
        for variant in final["variants"]:
            for replicate in variant["replicates"]:
                self.assertEqual(replicate["status"], "succeeded", replicate)
                self.assertTrue(replicate.get("measurementId"))
                manifest = storage.read_json(storage.run_path(replicate["runId"]) / "manifest.json")
                self.assertEqual(manifest["actualSeed"], replicate["seed"])

    async def test_create_ten_variants_with_replicates(self):
        p = project()
        variants = [{"parameters": {"step_etch_01": {"duration": 0.01 + index * 0.001}}, "replicates": 2, "seed": 100 + index} for index in range(10)]
        response = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={
                "name": "ten variants", "project": p, "flowId": "flow_exp_01",
                "recipeId": self.recipe_id, "recipeVersion": 1, "variants": variants,
            },
        )
        self.assertEqual(response[0], 201, response[2])
        document = json_body(response)
        self.assertEqual(len(document["variants"]), 10)
        self.assertTrue(all(len(variant["replicates"]) == 2 for variant in document["variants"]))
        fetched = await asgi_request(f"/api/v1/workflow/experiments/{document['experimentId']}")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(len(json_body(fetched)["variants"]), 10)

    async def test_delete_experiment_document(self):
        from backend.workflow.api import get_store

        p = project()
        response = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={
                "name": "待删除实验", "project": p, "flowId": "flow_exp_delete",
                "recipeId": self.recipe_id, "recipeVersion": 1,
                "variants": [{"parameters": {"step_etch_01": {"duration": 0.01}}, "replicates": 1, "seed": 5}],
            },
        )
        self.assertEqual(response[0], 201, response[2])
        experiment_id = json_body(response)["experimentId"]

        deleted = await asgi_request(
            f"/api/v1/workflow/experiments/{experiment_id}", method="DELETE"
        )
        self.assertEqual(deleted[0], 204, deleted[2])
        gone = await asgi_request(f"/api/v1/workflow/experiments/{experiment_id}")
        self.assertEqual(gone[0], 404, gone[2])
        again = await asgi_request(
            f"/api/v1/workflow/experiments/{experiment_id}", method="DELETE"
        )
        self.assertEqual(again[0], 404, again[2])

        # A running experiment is refused with a Chinese 409 instead of being
        # removed while its worker keeps writing the document.
        running = await asgi_request(
            "/api/v1/workflow/experiments", method="POST",
            payload={
                "name": "运行中实验", "project": p, "flowId": "flow_exp_delete",
                "recipeId": self.recipe_id, "recipeVersion": 1,
                "variants": [{"parameters": {"step_etch_01": {"duration": 0.01}}, "replicates": 1, "seed": 6}],
            },
        )
        self.assertEqual(running[0], 201, running[2])
        running_document = json_body(running)
        running_document["status"] = "running"
        get_store().save_experiment(running_document["experimentId"], running_document)
        refused = await asgi_request(
            f"/api/v1/workflow/experiments/{running_document['experimentId']}", method="DELETE"
        )
        self.assertEqual(refused[0], 409, refused[2])
        self.assertIn("实验运行中，先取消", json_body(refused)["detail"])
        still_there = await asgi_request(
            f"/api/v1/workflow/experiments/{running_document['experimentId']}"
        )
        self.assertEqual(still_there[0], 200, still_there[2])
        # Leave no running document behind for later tests.
        running_document["status"] = "planned"
        get_store().save_experiment(running_document["experimentId"], running_document)
        await asgi_request(
            f"/api/v1/workflow/experiments/{running_document['experimentId']}", method="DELETE"
        )



if __name__ == "__main__":
    unittest.main()
