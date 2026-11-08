"""Formal Measurement -> Segment binding evidence and append-only verification."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_metrology import REAL_SNAPSHOT  # noqa: E402
from tests.workflow_fixtures import binding_plan_2d, compiled_2d  # noqa: E402


class BindingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.compiled = compiled_2d()
        self.suffix = uuid.uuid4().hex[:6]
        self.run_id = uuid.uuid4().hex[:24]
        self.step_id = self.compiled.project.steps[0].id
        self.state_bytes = f"state-{self.suffix}".encode("utf-8")
        self.state_hash = hashlib.sha256(self.state_bytes).hexdigest()
        run_dir = storage.RUNS / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(REAL_SNAPSHOT.read_text(encoding="utf-8"))
        storage.atomic_json(run_dir / f"{self.step_id}.json", snapshot)
        (run_dir / f"state-{self.step_id}.vpsd").write_bytes(self.state_bytes)
        storage.atomic_json(
            run_dir / "manifest.json",
            {
                "schemaVersion": 7,
                "runId": self.run_id,
                "dimension": 2,
                "status": "succeeded",
                "normalizedProject": self.compiled.project.model_dump(mode="json", exclude_none=True),
                "snapshots": [
                    {
                        "stepId": self.step_id,
                        "stepIndex": 1,
                        "snapshot": f"{self.step_id}.json",
                        "nativeState": {
                            "fileName": f"state-{self.step_id}.vpsd",
                            "stateId": self.state_hash,
                            "sourceRunId": self.run_id,
                            "sourceStepId": self.step_id,
                        },
                    }
                ],
            },
        )
        self.metric_id = f"metric_bind_{self.suffix}"
        self.recipe_id = f"recipe_bind_{self.suffix}"
        metric = await asgi_request(
            "/api/v1/workflow/metrics",
            method="POST",
            payload={
                "schemaVersion": 1,
                "metricId": self.metric_id,
                "name": "binding depth",
                "unit": "um",
                "description": "",
            },
        )
        self.assertEqual(metric[0], 201, metric[2])
        recipe = await asgi_request(
            "/api/v1/workflow/metrology-recipes",
            method="POST",
            payload={
                "schemaVersion": 1,
                "recipeId": self.recipe_id,
                "version": 1,
                "metricId": self.metric_id,
                "name": "binding depth",
                "dimension": 2,
                "method": "etch_depth",
                "parameters": {"kind": "etch_depth", "x": 0.0, "referenceHeight": 0.0},
                "material": "Si",
            },
        )
        self.assertEqual(recipe[0], 201, recipe[2])
        registered = await asgi_request(
            "/api/v1/workflow/compiles",
            method="POST",
            payload=self.compiled.model_dump(mode="json", exclude_none=True),
        )
        self.assertEqual(registered[0], 201, registered[2])

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def _measurement(self):
        response = await asgi_request(
            "/api/v1/workflow/measurements/from-run",
            method="POST",
            payload={
                "recipeId": self.recipe_id,
                "recipeVersion": 1,
                "runId": self.run_id,
                "stepId": self.step_id,
            },
        )
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def _route(self, *, segment_project_hash: str | None = None):
        plan = binding_plan_2d(
            self.compiled,
            suffix=self.suffix,
            run_id=self.run_id,
            state_hash=self.state_hash,
        )
        payload = plan.model_dump(mode="json", exclude_none=True)
        if segment_project_hash is not None:
            payload["segments"][0]["segmentProjectHash"] = segment_project_hash
        response = await asgi_request("/api/v1/workflow/routes", method="POST", payload=payload)
        self.assertEqual(response[0], 201, response[2])
        return plan

    async def _bind(self, measurement_id: str, *, segment_id: str | None = None, run_id: str | None = None, step_id: str | None = None):
        plan = binding_plan_2d(
            self.compiled,
            suffix=self.suffix,
            run_id=self.run_id,
            state_hash=self.state_hash,
        )
        return await asgi_request(
            f"/api/v1/workflow/measurements/{measurement_id}/bind",
            method="POST",
            payload={
                "segmentId": segment_id or plan.segments[0].segmentId,
                "runId": run_id or self.run_id,
                "stepId": step_id or self.step_id,
            },
        )

    async def test_valid_binding_is_verified_and_equivalence_stays_not_evaluated(self):
        measurement = await self._measurement()
        await self._route()
        bound = await self._bind(measurement["measurementId"])
        self.assertEqual(bound[0], 200, bound[2])
        body = json_body(bound)
        self.assertEqual(body["bindingStatus"], "verified")
        self.assertEqual(body["equivalenceStatus"], "not_evaluated")
        self.assertEqual(body["bindingEvidence"]["execution"]["status"], "verified")
        self.assertEqual(body["bindingEvidence"]["targetStep"]["status"], "verified")
        self.assertEqual(body["bindingEvidence"]["sourceIdentity"]["status"], "verified")
        provenance = await asgi_request(f"/api/v1/workflow/measurements/{measurement['measurementId']}/provenance")
        self.assertEqual(provenance[0], 200, provenance[2])
        self.assertEqual(len(json_body(provenance)["verifications"]), 1)
        self.assertEqual(json_body(provenance)["verifications"][0]["status"], "verified")

    async def test_failed_different_candidate_does_not_revoke_verified_binding(self):
        measurement = await self._measurement()
        await self._route()
        first = await self._bind(measurement["measurementId"])
        self.assertEqual(json_body(first)["bindingStatus"], "verified")
        wrong_run = uuid.uuid4().hex[:24]
        second = await self._bind(measurement["measurementId"], run_id=wrong_run)
        self.assertEqual(second[0], 200, second[2])
        self.assertEqual(json_body(second)["bindingStatus"], "verified")
        provenance = await asgi_request(f"/api/v1/workflow/measurements/{measurement['measurementId']}/provenance")
        records = json_body(provenance)["verifications"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["status"], "failed")

    async def test_equivalence_rejects_payload_not_matching_actual_snapshot(self):
        measurement = await self._measurement()
        plan = await self._route()
        snapshot = json.loads((storage.RUNS / self.run_id / f"{self.step_id}.json").read_text(encoding="utf-8"))
        fake = json.loads(json.dumps(snapshot))
        fake["points"][0][1] += 1.0
        response = await asgi_request(
            "/api/v1/workflow/equivalence/check",
            method="POST",
            payload={
                "protocol": {"schemaVersion": 1, "protocolId": "proto_real", "version": 1, "pointTolerance": 0.01, "checkType": "same_segment_replay"},
                "left": fake,
                "right": snapshot,
                "leftRunId": self.run_id, "rightRunId": self.run_id,
                "leftSegmentId": plan.segments[0].segmentId, "rightSegmentId": plan.segments[0].segmentId,
                "leftStepId": self.step_id, "rightStepId": self.step_id,
            },
        )
        self.assertEqual(response[0], 200, response[2])
        self.assertEqual(json_body(response)["status"], "failed")
        self.assertTrue(any("actual run snapshot" in reason for reason in json_body(response)["reasons"]))

    async def test_snapshot_hash_mismatch_fails_binding(self):
        measurement = await self._measurement()
        await self._route()
        snapshot_path = storage.RUNS / self.run_id / f"{self.step_id}.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["points"][0][1] -= 1.0
        storage.atomic_json(snapshot_path, snapshot)
        bound = await self._bind(measurement["measurementId"])
        self.assertEqual(bound[0], 200, bound[2])
        self.assertEqual(json_body(bound)["bindingStatus"], "failed")
        self.assertTrue(json_body(bound)["bindingEvidence"]["sourceIdentity"]["reasons"])

    async def test_segment_project_hash_mismatch_fails_binding(self):
        measurement = await self._measurement()
        await self._route(segment_project_hash="a" * 64)
        bound = await self._bind(measurement["measurementId"])
        self.assertEqual(bound[0], 200, bound[2])
        self.assertEqual(json_body(bound)["bindingStatus"], "failed")
        self.assertTrue(json_body(bound)["bindingEvidence"]["execution"]["reasons"])

    async def test_source_step_mismatch_fails_binding(self):
        measurement = await self._measurement()
        await self._route()
        other_step = self.compiled.project.steps[1].id
        bound = await self._bind(measurement["measurementId"], step_id=other_step)
        self.assertEqual(bound[0], 200, bound[2])
        self.assertEqual(json_body(bound)["bindingStatus"], "failed")
        reasons = json_body(bound)["bindingEvidence"]["sourceIdentity"]["reasons"]
        self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()
