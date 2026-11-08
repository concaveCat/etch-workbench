"""P0-3 metrology persistence and historical-run measurement API."""

from __future__ import annotations

import json
import os
import unittest
import uuid
os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.workflow.api import get_store  # noqa: E402
from backend.workflow.store import SCHEMA_VERSION  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_metrology import REAL_SNAPSHOT, trench  # noqa: E402


RUN_ID = "52e98c3ceddfd77e1de08b9f"
STEP_ID = "gpu_geometry_process"


class MetrologyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        run_dir = storage.RUNS / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(REAL_SNAPSHOT.read_text(encoding="utf-8"))
        storage.atomic_json(run_dir / f"{STEP_ID}.json", snapshot)
        storage.atomic_json(
            run_dir / "manifest.json",
            {
                "schemaVersion": 7,
                "runId": RUN_ID,
                "dimension": 2,
                "snapshots": [
                    {
                        "stepId": STEP_ID,
                        "snapshot": f"{STEP_ID}.json",
                        "nativeState": {"stateId": "a" * 64},
                    }
                ],
            },
        )

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    def suffix(self) -> str:
        return uuid.uuid4().hex[:6]

    async def _register_metric(self, metric_id: str, unit: str = "um"):
        response = await asgi_request(
            "/api/v1/workflow/metrics",
            method="POST",
            payload={
                "schemaVersion": 1,
                "metricId": metric_id,
                "name": metric_id,
                "unit": unit,
                "description": "",
            },
        )
        self.assertEqual(response[0], 201, response[2])

    async def _register_recipe(self, recipe_id: str, metric_id: str, version: int, method: str, parameters: dict):
        response = await asgi_request(
            "/api/v1/workflow/metrology-recipes",
            method="POST",
            payload={
                "schemaVersion": 1,
                "recipeId": recipe_id,
                "version": version,
                "metricId": metric_id,
                "name": recipe_id,
                "dimension": 2,
                "method": method,
                "parameters": parameters,
                "material": "Si",
            },
        )
        self.assertEqual(response[0], 201, response[2])

    async def test_measure_snapshot_persists_and_lists(self):
        suffix = self.suffix()
        metric_id = f"metric_cd_{suffix}"
        recipe_id = f"recipe_cd_{suffix}"
        await self._register_metric(metric_id, unit="nm")
        await self._register_recipe(
            recipe_id, metric_id, 1, "cd_at_height",
            {"kind": "cd_at_height", "height": -1.0, "pairSelection": "outermost"},
        )
        response = await asgi_request(
            "/api/v1/workflow/measurements",
            method="POST",
            payload={"recipeId": recipe_id, "recipeVersion": 1, "snapshot": trench()},
        )
        self.assertEqual(response[0], 201, response[2])
        body = json_body(response)
        self.assertEqual(body["status"], "measured")
        self.assertAlmostEqual(body["value"], 2000.0)
        self.assertEqual(body["unit"], "nm")
        self.assertEqual(body["uncertaintyStatus"], "not_evaluated")

        fetched = await asgi_request(f"/api/v1/workflow/measurements/{body['measurementId']}")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched)["measurementId"], body["measurementId"])

        listed = await asgi_request(f"/api/v1/workflow/measurements?recipe_id={recipe_id}")
        self.assertEqual(listed[0], 200, listed[2])
        self.assertEqual(len(json_body(listed)["measurements"]), 1)

    async def test_no_intersection_is_explicit_and_persisted(self):
        suffix = self.suffix()
        metric_id = f"metric_none_{suffix}"
        recipe_id = f"recipe_none_{suffix}"
        await self._register_metric(metric_id)
        await self._register_recipe(
            recipe_id, metric_id, 1, "cd_at_height",
            {"kind": "cd_at_height", "height": 1.0, "pairSelection": "outermost"},
        )
        response = await asgi_request(
            "/api/v1/workflow/measurements",
            method="POST",
            payload={"recipeId": recipe_id, "recipeVersion": 1, "snapshot": trench()},
        )
        self.assertEqual(response[0], 201, response[2])
        body = json_body(response)
        self.assertEqual(body["status"], "no_intersection")
        self.assertIsNone(body["value"])
        self.assertTrue(body["reason"])

    async def test_recipe_version_produces_a_new_measurement_record(self):
        suffix = self.suffix()
        metric_id = f"metric_ver_{suffix}"
        recipe_id = f"recipe_ver_{suffix}"
        await self._register_metric(metric_id)
        parameters = {"kind": "cd_at_height", "height": -1.0, "pairSelection": "outermost"}
        await self._register_recipe(recipe_id, metric_id, 1, "cd_at_height", parameters)
        await self._register_recipe(recipe_id, metric_id, 2, "cd_at_height", parameters)

        first = await asgi_request(
            "/api/v1/workflow/measurements",
            method="POST",
            payload={"recipeId": recipe_id, "recipeVersion": 1, "snapshot": trench()},
        )
        second = await asgi_request(
            "/api/v1/workflow/measurements",
            method="POST",
            payload={"recipeId": recipe_id, "recipeVersion": 2, "snapshot": trench()},
        )
        self.assertEqual(first[0], 201, first[2])
        self.assertEqual(second[0], 201, second[2])
        first_body, second_body = json_body(first), json_body(second)
        self.assertNotEqual(first_body["measurementId"], second_body["measurementId"])
        self.assertEqual(first_body["recipeVersion"], 1)
        self.assertEqual(second_body["recipeVersion"], 2)
        listed = await asgi_request(f"/api/v1/workflow/measurements?recipe_id={recipe_id}")
        self.assertEqual(len(json_body(listed)["measurements"]), 2)

    async def test_measure_from_historical_run_snapshot(self):
        suffix = self.suffix()
        metric_id = f"metric_depth_{suffix}"
        recipe_id = f"recipe_depth_{suffix}"
        await self._register_metric(metric_id)
        await self._register_recipe(
            recipe_id, metric_id, 1, "etch_depth",
            {"kind": "etch_depth", "x": 0.0, "referenceHeight": 0.0},
        )
        response = await asgi_request(
            "/api/v1/workflow/measurements/from-run",
            method="POST",
            payload={"recipeId": recipe_id, "recipeVersion": 1, "runId": RUN_ID, "stepId": STEP_ID},
        )
        self.assertEqual(response[0], 201, response[2])
        body = json_body(response)
        self.assertEqual(body["status"], "measured")
        self.assertEqual(body["sourceRunId"], RUN_ID)
        self.assertEqual(body["sourceStepId"], STEP_ID)
        self.assertEqual(body["sourceStateId"], "a" * 64)
        self.assertGreater(body["value"], 0)
        self.assertRegex(body["sourceSnapshotHash"], r"^[a-f0-9]{64}$")

    async def test_store_schema_version_is_explicit(self):
        self.assertEqual(get_store().schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
