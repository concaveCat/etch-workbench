"""P4-2 measurement-driven branch decisions, replay and error boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.workflow.adoption import adopt_project  # noqa: E402
from backend.workflow.compiler import compile_flow  # noqa: E402
from backend.workflow.decisions import _build_target_compile  # noqa: E402
from backend.workflow.store import SCHEMA_VERSION, WorkflowStore  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402


def three_step_project(flow_id: str) -> dict:
    return {
        "schemaVersion": 7,
        "name": "decision 2D",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.1, "xExtent": 2.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            {
                "id": "step_etch_01", "name": "first", "enabled": True, "operation": "etch",
                "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1, "rateMode": "uniform",
            },
            {
                "id": "step_etch_02", "name": "second", "enabled": True, "operation": "etch",
                "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1, "rateMode": "uniform",
            },
            {
                "id": "step_etch_03", "name": "tail", "enabled": True, "operation": "etch",
                "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1, "rateMode": "uniform",
            },
        ],
        "solver": {
            "timeStepRatio": 0.25, "raysPerPoint": 16, "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 20261030}, "enginePreference": "cpu",
            "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


class MeasurementDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:6]
        self.flow_id = f"flow_dec_{self.suffix}"
        adopted = await asgi_request(
            "/api/v1/workflow/flows/from-project",
            method="POST",
            payload={"project": three_step_project(self.flow_id), "flowId": self.flow_id},
        )
        self.assertEqual(adopted[0], 201, adopted[2])
        self.adopted = json_body(adopted)
        self.compile_id = self.adopted["compileId"]
        source_map = self.adopted["compile"]["sourceMap"]
        self.step_ids = {
            entry["authoredStepId"]: entry["stepId"] for entry in source_map
        }
        self.prefix_step_id = self.step_ids["step_etch_02"]
        self.tail_step_id = self.step_ids["step_etch_03"]

        route = await asgi_request(
            "/api/v1/workflow/routes/from-compile",
            method="POST",
            payload={
                "compileId": self.compile_id,
                "name": "decision route",
                "splitAfterStepId": self.prefix_step_id,
            },
        )
        self.assertEqual(route[0], 201, route[2])
        self.route = json_body(route)["plan"]
        self.route_id = self.route["route"]["routeId"]
        self.prefix_segment = self.route["segments"][0]
        self.assertEqual(self.prefix_segment["throughStepId"], self.prefix_step_id)

        self.metric_id = f"metric_dec_{self.suffix}"
        self.recipe_id = f"recipe_dec_{self.suffix}"
        for path, payload in (
            (
                "/api/v1/workflow/metrics",
                {
                    "schemaVersion": 1, "metricId": self.metric_id,
                    "name": "decision depth", "unit": "um", "description": "",
                },
            ),
            (
                "/api/v1/workflow/metrology-recipes",
                {
                    "schemaVersion": 1, "recipeId": self.recipe_id, "version": 1,
                    "metricId": self.metric_id, "name": "decision depth",
                    "dimension": 2, "method": "etch_depth",
                    "parameters": {"kind": "etch_depth", "x": 0.0, "referenceHeight": 0.0},
                    "material": "Si",
                },
            ),
        ):
            response = await asgi_request(path, method="POST", payload=payload)
            self.assertIn(response[0], (201, 409), response[2])

        first = await self._execute(self.prefix_segment["segmentId"])
        self.assertEqual(first["status"], "succeeded", first)
        self.executed_prefix = first

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def _wait_segment(self, segment_id: str, timeout: float = 60.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        body: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            response = await asgi_request(f"/api/v1/workflow/segments/{segment_id}")
            self.assertEqual(response[0], 200, response[2])
            body = json_body(response)
            if body["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                return body
            await asyncio.sleep(0.2)
        self.fail(f"segment {segment_id} did not finish: {body}")

    async def _execute(self, segment_id: str) -> dict:
        response = await asgi_request(
            f"/api/v1/workflow/segments/{segment_id}/execute", method="POST", payload={}
        )
        self.assertEqual(response[0], 200, response[2])
        return await self._wait_segment(segment_id)

    def _decision_payload(self, *, operator: str = "gt", threshold: float = 0.0,
                          true_parameters: dict, false_parameters: dict,
                          start_after: str | None = None,
                          through: str | None = None,
                          workflow_run_id: str | None = None,
                          recipe_id: str | None = None) -> dict:
        payload = {
            "startAfterStepId": self.prefix_step_id if start_after is None else start_after,
            "recipeId": recipe_id or self.recipe_id,
            "recipeVersion": 1,
            "operator": operator,
            "threshold": threshold,
            "branches": {
                "true": {"parameters": true_parameters},
                "false": {"parameters": false_parameters},
            },
        }
        if through is not None:
            payload["branchThroughStepId"] = through
        if workflow_run_id is not None:
            payload["workflowRunId"] = workflow_run_id
        return payload

    async def _decide(self, **kwargs) -> dict:
        response = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(**kwargs),
        )
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)

    async def _create_route(self, name: str, split_after: str | None) -> dict:
        response = await asgi_request(
            "/api/v1/workflow/routes/from-compile",
            method="POST",
            payload={
                "compileId": self.compile_id,
                "name": name,
                "splitAfterStepId": split_after,
            },
        )
        self.assertEqual(response[0], 201, response[2])
        return json_body(response)["plan"]

    @staticmethod
    def _db_path():
        return storage.VAR / "workflow" / "workflow.sqlite3"

    def _read_decision(self, decision_id: str) -> dict:
        with sqlite3.connect(self._db_path()) as connection:
            row = connection.execute(
                "SELECT document_json FROM measurement_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def _set_decision_document(self, decision_id: str, document: dict) -> None:
        with sqlite3.connect(self._db_path()) as connection:
            connection.execute(
                "UPDATE measurement_decisions SET document_json = ? WHERE decision_id = ?",
                (json.dumps(document), decision_id),
            )

    def _rewrite_measurement(self, measurement_id: str, sql: str, values: tuple) -> None:
        with sqlite3.connect(self._db_path()) as connection:
            connection.execute(
                f"UPDATE measurements SET {sql} WHERE measurement_id = ?",
                (*values, measurement_id),
            )

    async def _measurement_count(self, run_id: str) -> int:
        response = await asgi_request(f"/api/v1/workflow/measurements?run_id={run_id}")
        self.assertEqual(response[0], 200, response[2])
        return len(json_body(response)["measurements"])

    async def _target_step(self, compile_id: str, authored_step_id: str) -> dict:
        response = await asgi_request(f"/api/v1/workflow/compiles/{compile_id}")
        self.assertEqual(response[0], 200, response[2])
        compiled = json_body(response)
        step_id = next(
            entry["stepId"]
            for entry in compiled["sourceMap"]
            if entry["authoredStepId"] == authored_step_id
        )
        return next(step for step in compiled["project"]["steps"] if step["id"] == step_id)

    async def test_measurement_selects_both_directions_and_records_overrides(self):
        true_body = await self._decide(
            operator="gt", threshold=0.0,
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        decision = true_body["decision"]
        self.assertEqual(decision["chosenBranch"], "true")
        self.assertTrue(decision["result"])
        self.assertEqual(decision["measurementId"], true_body["measurement"]["measurementId"])
        self.assertEqual(true_body["measurement"]["status"], "measured")
        self.assertAlmostEqual(decision["measuredValue"], true_body["measurement"]["value"])
        self.assertGreater(decision["measuredValue"], 0.0)
        self.assertRegex(decision["workflowRunId"], r"^wfr_")
        self.assertEqual(true_body["workflowRunId"], decision["workflowRunId"])
        self.assertEqual(
            true_body["resolvedParameters"],
            {"step_etch_03": {"duration": 0.03}},
        )
        self.assertEqual(decision["measurementBindingStatus"], "verified")
        self.assertIsNone(decision["replayOf"])

        gc = await asgi_request(
            "/api/v1/workflow/gc/dry-run", method="POST", payload={}
        )
        self.assertEqual(gc[0], 200, gc[2])
        gc_body = json_body(gc)
        self.assertIn(self.executed_prefix["runId"], gc_body["referenced"])
        self.assertNotIn(
            self.executed_prefix["runId"],
            {item["artifactId"] for item in gc_body["candidates"]},
        )

        target = await self._target_step(decision["targetCompileId"], "step_etch_03")
        self.assertAlmostEqual(target["duration"], 0.03)

        compile_response = await asgi_request(
            f"/api/v1/workflow/compiles/{decision['targetCompileId']}"
        )
        self.assertEqual(compile_response[0], 200, compile_response[2])
        record = json_body(compile_response)["compileRecord"]
        self.assertTrue(any(
            item.get("stepId") == "step_etch_03"
            and item.get("parameter") == "duration"
            and abs(float(item.get("value")) - 0.03) < 1e-12
            for item in record["overrides"]
        ))
        self.assertTrue(any(
            item.get("name") == "step_etch_03.duration"
            and abs(float(item.get("value")) - 0.03) < 1e-12
            for item in record["variableSet"]
        ))

        plan = true_body["plan"]
        attempt = next(
            item for item in plan["attempts"]
            if item["attemptId"] == decision["branchAttemptId"]
        )
        segment = next(
            item for item in plan["segments"]
            if item["segmentId"] == decision["branchSegmentId"]
        )
        self.assertEqual(attempt["compileId"], decision["targetCompileId"])
        self.assertEqual(segment["sourceCompileId"], decision["targetCompileId"])
        self.assertEqual(segment["startAfterStepId"], self.prefix_step_id)
        self.assertEqual(segment["status"], "planned")

        false_body = await self._decide(
            operator="gt", threshold=1.0,
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        false_decision = false_body["decision"]
        self.assertEqual(false_decision["chosenBranch"], "false")
        self.assertFalse(false_decision["result"])
        self.assertEqual(
            false_decision["resolvedParameters"],
            {"step_etch_03": {"duration": 0.2}},
        )
        self.assertEqual(
            false_decision["workflowRunId"], decision["workflowRunId"]
        )
        false_target = await self._target_step(
            false_decision["targetCompileId"], "step_etch_03"
        )
        self.assertAlmostEqual(false_target["duration"], 0.2)

        listed = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions"
        )
        self.assertEqual(listed[0], 200, listed[2])
        self.assertEqual(len(json_body(listed)["decisions"]), 2)
        fetched = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{decision['decisionId']}"
        )
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched)["decisionId"], decision["decisionId"])

        tail = await self._execute(decision["branchSegmentId"])
        self.assertEqual(tail["status"], "succeeded", tail)

    async def test_replay_uses_recorded_decision_without_measuring(self):
        created = await self._decide(
            operator="gt", threshold=0.0,
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        original = created["decision"]
        run_id = original["sourceRunId"]
        before = await asgi_request(f"/api/v1/workflow/measurements?run_id={run_id}")
        self.assertEqual(before[0], 200, before[2])
        count_before = len(json_body(before)["measurements"])

        response = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{original['decisionId']}/replay",
            method="POST",
            payload={},
        )
        self.assertEqual(response[0], 201, response[2])
        replayed = json_body(response)
        decision = replayed["decision"]
        self.assertNotEqual(decision["decisionId"], original["decisionId"])
        self.assertEqual(decision["replayOf"], original["decisionId"])
        self.assertEqual(decision["measurementId"], original["measurementId"])
        self.assertEqual(decision["chosenBranch"], original["chosenBranch"])
        self.assertEqual(decision["resolvedParameters"], original["resolvedParameters"])
        self.assertEqual(decision["targetCompileId"], original["targetCompileId"])
        self.assertEqual(decision["workflowRunId"], original["workflowRunId"])
        self.assertEqual(replayed["measurement"]["measurementId"], original["measurementId"])
        self.assertEqual(replayed["workflowRunId"], original["workflowRunId"])
        self.assertEqual(
            replayed["resolvedParameters"], {"step_etch_03": {"duration": 0.03}}
        )
        self.assertNotEqual(decision["branchAttemptId"], original["branchAttemptId"])

        after = await asgi_request(f"/api/v1/workflow/measurements?run_id={run_id}")
        self.assertEqual(after[0], 200, after[2])
        self.assertEqual(len(json_body(after)["measurements"]), count_before)
        measurement = await asgi_request(
            f"/api/v1/workflow/measurements/{original['measurementId']}"
        )
        self.assertEqual(measurement[0], 200, measurement[2])
        self.assertEqual(
            json_body(measurement)["measurementId"], original["measurementId"]
        )

        plan = replayed["plan"]
        attempt = next(
            item for item in plan["attempts"]
            if item["attemptId"] == decision["branchAttemptId"]
        )
        segment = next(
            item for item in plan["segments"]
            if item["segmentId"] == decision["branchSegmentId"]
        )
        self.assertEqual(attempt["compileId"], original["targetCompileId"])
        self.assertEqual(segment["sourceCompileId"], original["targetCompileId"])
        self.assertEqual(segment["startAfterStepId"], original["startAfterStepId"])

    async def test_replayed_branch_executes_to_the_same_result(self):
        created = await self._decide(
            operator="gt", threshold=0.0,
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        original = created["decision"]
        first = await self._execute(original["branchSegmentId"])
        self.assertEqual(first["status"], "succeeded", first)
        self.assertRegex(first["resultDigest"], r"^[a-f0-9]{64}$")

        response = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{original['decisionId']}/replay",
            method="POST",
            payload={},
        )
        self.assertEqual(response[0], 201, response[2])
        replayed = json_body(response)["decision"]
        self.assertEqual(replayed["targetCompileId"], original["targetCompileId"])
        second = await self._execute(replayed["branchSegmentId"])
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(second["resultDigest"], first["resultDigest"])
        self.assertEqual(
            second["endCheckpointRef"]["stateHash"],
            first["endCheckpointRef"]["stateHash"],
        )

    async def test_error_boundaries_are_explicit(self):
        # No succeeded prefix on a fresh route.
        route = await asgi_request(
            "/api/v1/workflow/routes/from-compile",
            method="POST",
            payload={"compileId": self.compile_id, "name": "empty prefix"},
        )
        self.assertEqual(route[0], 201, route[2])
        empty_route_id = json_body(route)["plan"]["route"]["routeId"]
        empty = await asgi_request(
            f"/api/v1/workflow/routes/{empty_route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_03": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
            ),
        )
        self.assertEqual(empty[0], 404, empty[2])

        # A non-measured recipe must not pick a branch.
        bad_metric = f"metric_none_{self.suffix}"
        bad_recipe = f"recipe_none_{self.suffix}"
        for path, payload in (
            (
                "/api/v1/workflow/metrics",
                {"schemaVersion": 1, "metricId": bad_metric, "name": "none", "unit": "um"},
            ),
            (
                "/api/v1/workflow/metrology-recipes",
                {
                    "schemaVersion": 1, "recipeId": bad_recipe, "version": 1,
                    "metricId": bad_metric, "name": "outside", "dimension": 2,
                    "method": "etch_depth",
                    "parameters": {"kind": "etch_depth", "x": 999.0, "referenceHeight": 0.0},
                    "material": "Si",
                },
            ),
        ):
            response = await asgi_request(path, method="POST", payload=payload)
            self.assertIn(response[0], (201, 409), response[2])
        non_measured = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload={
                **self._decision_payload(
                    true_parameters={"step_etch_03": {"duration": 0.03}},
                    false_parameters={"step_etch_03": {"duration": 0.2}},
                ),
                "recipeId": bad_recipe,
            },
        )
        self.assertEqual(non_measured[0], 422, non_measured[2])

        # Invalid operator is rejected by the request contract.
        invalid_operator = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload={
                **self._decision_payload(
                    true_parameters={"step_etch_03": {"duration": 0.03}},
                    false_parameters={"step_etch_03": {"duration": 0.2}},
                ),
                "operator": "between",
            },
        )
        self.assertEqual(invalid_operator[0], 422, invalid_operator[2])

        # Missing branch parameters and unknown authored steps are 422.
        missing_branch = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload={
                "startAfterStepId": self.prefix_step_id,
                "recipeId": self.recipe_id,
                "recipeVersion": 1,
                "operator": "gt",
                "threshold": 0.0,
                "branches": {"true": {}},
            },
        )
        self.assertEqual(missing_branch[0], 422, missing_branch[2])

        unknown_step = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_missing": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
            ),
        )
        self.assertEqual(unknown_step[0], 422, unknown_step[2])

        prefix_override = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_01": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
            ),
        )
        self.assertEqual(prefix_override[0], 422, prefix_override[2])

        # Cross-route replay is not allowed.
        created = await self._decide(
            operator="gt", threshold=0.0,
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        cross = await asgi_request(
            f"/api/v1/workflow/routes/{empty_route_id}/measurement-decisions/"
            f"{created['decision']['decisionId']}/replay",
            method="POST",
            payload={},
        )
        self.assertEqual(cross[0], 404, cross[2])

    async def test_chained_decisions_share_one_workflow_run_and_replay(self):
        first_step = self.step_ids["step_etch_01"]
        middle = self.step_ids["step_etch_02"]
        tail = self.step_ids["step_etch_03"]
        route = await self._create_route("chained decisions", first_step)
        route_id = route["route"]["routeId"]
        prefix = route["segments"][0]
        self.assertEqual(prefix["throughStepId"], first_step)
        self.assertEqual((await self._execute(prefix["segmentId"]))["status"], "succeeded")

        base = self._decision_payload(
            start_after=first_step,
            true_parameters={"step_etch_02": {"duration": 0.03}},
            false_parameters={"step_etch_02": {"duration": 0.2}},
        )
        # The reported branch end bounds which authored steps may change.
        after_end = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions",
            method="POST",
            payload={
                **base,
                "branchThroughStepId": middle,
                "branches": {
                    "true": {"parameters": {"step_etch_03": {"duration": 0.04}}},
                    "false": {"parameters": {"step_etch_03": {"duration": 0.5}}},
                },
            },
        )
        self.assertEqual(after_end[0], 422, after_end[2])
        self.assertIn("after the branch end", after_end[2].decode())
        before_start = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions",
            method="POST",
            payload={**base, "branchThroughStepId": first_step},
        )
        self.assertEqual(before_start[0], 422, before_start[2])

        first = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions",
            method="POST",
            payload={**base, "branchThroughStepId": middle},
        )
        self.assertEqual(first[0], 201, first[2])
        first_body = json_body(first)
        decision1 = first_body["decision"]
        self.assertEqual(decision1["sourceAttemptId"], prefix["attemptId"])
        self.assertEqual(decision1["branchThroughStepId"], middle)
        self.assertEqual(decision1["seed"], 20261030)
        self.assertRegex(decision1["sourcePrefixDigest"], r"^[a-f0-9]{64}$")
        self.assertRegex(decision1["sourceCheckpointHash"], r"^[a-f0-9]{64}$")
        self.assertRegex(decision1["sourceCompileHash"], r"^[a-f0-9]{64}$")
        self.assertRegex(decision1["targetCompileHash"], r"^[a-f0-9]{64}$")
        self.assertEqual(decision1["measurementBindingStatus"], "verified")
        self.assertEqual(decision1["measurementEquivalenceStatus"], "not_evaluated")
        self.assertEqual(
            decision1["candidateParameters"]["false"]["parameters"],
            {"step_etch_02": {"duration": 0.2}},
        )
        branch1 = next(
            item for item in first_body["plan"]["segments"]
            if item["segmentId"] == decision1["branchSegmentId"]
        )
        self.assertEqual(branch1["throughStepId"], middle)
        self.assertEqual((await self._execute(branch1["segmentId"]))["status"], "succeeded")

        second = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                start_after=middle,
                true_parameters={"step_etch_03": {"duration": 0.04}},
                false_parameters={"step_etch_03": {"duration": 0.5}},
            ),
        )
        self.assertEqual(second[0], 201, second[2])
        second_body = json_body(second)
        decision2 = second_body["decision"]
        self.assertEqual(decision2["workflowRunId"], decision1["workflowRunId"])
        self.assertEqual(decision2["sourceSegmentId"], decision1["branchSegmentId"])
        self.assertEqual(decision2["branchThroughStepId"], tail)
        reuses = [
            item for item in second_body["plan"]["reuse"]
            if item["attemptId"] == decision2["branchAttemptId"]
        ]
        self.assertEqual(
            [(item["fromStepId"], item["toStepId"]) for item in reuses],
            [(first_step, first_step), (middle, middle)],
        )
        self.assertEqual(
            (await self._execute(decision2["branchSegmentId"]))["status"], "succeeded"
        )

        runs = json_body(
            await asgi_request(f"/api/v1/workflow/routes/{route_id}/workflow-runs")
        )
        self.assertEqual(len(runs["runs"]), 1)
        summary = runs["runs"][0]
        self.assertEqual(summary["workflowRunId"], decision1["workflowRunId"])
        self.assertEqual(summary["decisionCount"], 2)
        self.assertEqual(summary["replayCount"], 0)
        self.assertEqual(
            summary["decisionIds"], [decision1["decisionId"], decision2["decisionId"]]
        )
        detail = json_body(
            await asgi_request(
                f"/api/v1/workflow/routes/{route_id}/workflow-runs/"
                f"{decision1['workflowRunId']}"
            )
        )
        self.assertEqual(
            [item["decisionId"] for item in detail["decisions"]],
            [decision1["decisionId"], decision2["decisionId"]],
        )
        self.assertEqual(len(detail["attempts"]), 3)
        self.assertEqual(len(detail["segments"]), 3)

        replayed = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions/"
            f"{decision2['decisionId']}/replay",
            method="POST",
            payload={},
        )
        self.assertEqual(replayed[0], 201, replayed[2])
        replay_decision = json_body(replayed)["decision"]
        self.assertEqual(replay_decision["workflowRunId"], decision1["workflowRunId"])
        self.assertEqual(replay_decision["targetCompileId"], decision2["targetCompileId"])
        self.assertEqual(replay_decision["branchThroughStepId"], tail)
        self.assertEqual(replay_decision["sourceSegmentId"], decision1["branchSegmentId"])

    async def test_workflow_run_id_can_start_a_new_run_but_not_cross_routes(self):
        first = await self._decide(
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        run_a = first["decision"]["workflowRunId"]
        requested = f"wfr_{uuid.uuid4().hex[:16]}"
        created = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_03": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
                workflow_run_id=requested,
            ),
        )
        self.assertEqual(created[0], 201, created[2])
        self.assertEqual(json_body(created)["workflowRunId"], requested)
        self.assertNotEqual(requested, run_a)

        continued = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_03": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
                workflow_run_id=run_a,
            ),
        )
        self.assertEqual(continued[0], 201, continued[2])
        self.assertEqual(json_body(continued)["workflowRunId"], run_a)

        other = await self._create_route("other route", self.prefix_step_id)
        cross = await asgi_request(
            f"/api/v1/workflow/routes/{other['route']['routeId']}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_03": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
                workflow_run_id=run_a,
            ),
        )
        self.assertEqual(cross[0], 409, cross[2])
        self.assertIn("already belongs to route", cross[2].decode())

    async def test_decision_requires_verified_measurement_binding(self):
        run_id = self.executed_prefix["runId"]
        with sqlite3.connect(self._db_path()) as connection:
            connection.execute(
                "UPDATE segments SET segment_project_hash = ? WHERE segment_id = ?",
                ("0" * 64, self.prefix_segment["segmentId"]),
            )
        response = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload=self._decision_payload(
                true_parameters={"step_etch_03": {"duration": 0.03}},
                false_parameters={"step_etch_03": {"duration": 0.2}},
            ),
        )
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("not bound", response[2].decode())

        plan = json_body(
            await asgi_request(f"/api/v1/workflow/routes/{self.route_id}")
        )["plan"]
        self.assertEqual(len(plan["attempts"]), 1)
        self.assertEqual(len(plan["segments"]), 2)
        decisions = json_body(
            await asgi_request(
                f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions"
            )
        )
        self.assertEqual(decisions["decisions"], [])
        measurements = json_body(
            await asgi_request(f"/api/v1/workflow/measurements?run_id={run_id}")
        )["measurements"]
        self.assertTrue(any(item["bindingStatus"] == "failed" for item in measurements))

    async def test_replay_rejects_tampered_or_stale_records(self):
        created = await self._decide(
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        decision = created["decision"]
        decision_id = decision["decisionId"]
        path = (
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{decision_id}/replay"
        )
        original_document = self._read_decision(decision_id)

        def replay():
            return asgi_request(path, method="POST", payload={})

        def tampered(mutate) -> dict:
            document = json.loads(json.dumps(original_document))
            mutate(document)
            self._set_decision_document(decision_id, document)
            return document

        tampered(lambda document: document.update(result=not document["result"]))
        response = await replay()
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("result does not match", response[2].decode())

        tampered(lambda document: document.update(
            chosenBranch="false" if document["chosenBranch"] == "true" else "true"
        ))
        response = await replay()
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("chosen branch", response[2].decode())

        tampered(lambda document: document["resolvedParameters"]["step_etch_03"].update(
            duration=0.99
        ))
        response = await replay()
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("branch parameters", response[2].decode())

        tampered(lambda document: document.update(targetCompileId="c_" + "0" * 24))
        response = await replay()
        self.assertEqual(response[0], 409, response[2])

        self._set_decision_document(decision_id, original_document)
        self._rewrite_measurement(
            decision["measurementId"], "value = ?", (decision["measuredValue"] + 1.0,)
        )
        response = await replay()
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("measured value", response[2].decode())

        self._rewrite_measurement(
            decision["measurementId"], "value = ?", (decision["measuredValue"],)
        )
        self._rewrite_measurement(
            decision["measurementId"], "binding_status = ?", ("failed",)
        )
        response = await replay()
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("no longer bound", response[2].decode())

        self._rewrite_measurement(
            decision["measurementId"], "binding_status = ?", ("verified",)
        )
        response = await replay()
        self.assertEqual(response[0], 201, response[2])

    async def test_replay_rejects_record_hash_tampering_even_when_branch_is_unchanged(self):
        created = await self._decide(
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
            operator="gt",
            threshold=0.0,
        )
        decision = created["decision"]
        self.assertEqual(decision["chosenBranch"], "true")
        self.assertIn("recordHash", decision)
        original_document = self._read_decision(decision["decisionId"])
        self.assertEqual(original_document["recordHash"], decision["recordHash"])
        path = (
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{decision['decisionId']}/replay"
        )

        # 0.0 -> 0.001 still leaves the branch at "true", so the old replay
        # self-consistency check alone would have accepted the tampering.
        tampered = json.loads(json.dumps(original_document))
        tampered["threshold"] = 0.001
        self._set_decision_document(decision["decisionId"], tampered)
        response = await asgi_request(path, method="POST", payload={})
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("recordHash", response[2].decode())
        for read_path in (
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions/"
            f"{decision['decisionId']}",
        ):
            response = await asgi_request(read_path)
            self.assertEqual(response[0], 409, response[2])
            self.assertIn("recordHash", response[2].decode())
        with sqlite3.connect(self._db_path()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM measurement_decisions WHERE route_id = ?",
                (self.route_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

        # Records written before recordHash existed cannot be replayed; the
        # policy is explicit rejection rather than silently trusting them.
        legacy = json.loads(json.dumps(original_document))
        legacy.pop("recordHash")
        self._set_decision_document(decision["decisionId"], legacy)
        response = await asgi_request(path, method="POST", payload={})
        self.assertEqual(response[0], 409, response[2])
        self.assertIn("no recordHash", response[2].decode())

    async def test_auto_prefix_without_suffix_and_nonfinite_threshold_are_rejected(self):
        route = await self._create_route("single segment", None)
        route_id = route["route"]["routeId"]
        self.assertEqual(len(route["segments"]), 1)
        self.assertEqual(
            (await self._execute(route["segments"][0]["segmentId"]))["status"],
            "succeeded",
        )
        payload = self._decision_payload(
            true_parameters={"step_etch_03": {"duration": 0.03}},
            false_parameters={"step_etch_03": {"duration": 0.2}},
        )
        payload.pop("startAfterStepId")
        auto = await asgi_request(
            f"/api/v1/workflow/routes/{route_id}/measurement-decisions",
            method="POST",
            payload=payload,
        )
        self.assertEqual(auto[0], 409, auto[2])
        self.assertIn("no succeeded prefix with a suffix", auto[2].decode())

        run_id = self.executed_prefix["runId"]
        count = await self._measurement_count(run_id)
        nonfinite = await asgi_request(
            f"/api/v1/workflow/routes/{self.route_id}/measurement-decisions",
            method="POST",
            payload={
                **self._decision_payload(
                    true_parameters={"step_etch_03": {"duration": 0.03}},
                    false_parameters={"step_etch_03": {"duration": 0.2}},
                ),
                "threshold": float("nan"),
            },
        )
        self.assertEqual(nonfinite[0], 422, nonfinite[2])
        self.assertEqual(await self._measurement_count(run_id), count)


class DecisionCompileProvenanceTests(unittest.TestCase):
    def test_target_compile_keeps_source_identity_and_chained_overrides(self):
        suffix = uuid.uuid4().hex[:6]
        path = storage.VAR / f"test-workflow-decisions-provenance-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        flow_id = f"flow_prov_{suffix}"
        project = three_step_project(flow_id)
        flow, binding, plain = adopt_project(project, flow_id=flow_id)
        store.register_binding(binding)
        store.register_compile(plain)
        variable_set = [{"name": "rate", "value": 0.1, "source": "override"}]
        overrides = [{"name": "rate", "value": 0.1, "source": "request"}]
        varied = compile_flow(
            flow,
            binding,
            variable_set=variable_set,
            overrides=overrides,
            compiler_version="0.0.9",
            engine_versions={"cpu": "test-engine"},
        )
        store.register_compile(varied)

        resolved = {"step_etch_03": {"duration": 0.03}}
        target = _build_target_compile(store, varied, resolved)
        target_map = {entry.authoredStepId: entry for entry in target.sourceMap}
        self.assertEqual(
            target_map["step_etch_03"].parameterProvenance["duration"].kind,
            "variable",
        )
        self.assertEqual(
            target_map["step_etch_03"].parameterProvenance["duration"].source,
            "measurement-decision",
        )
        self.assertEqual(
            target_map["step_etch_03"].parameterProvenance["duration"].resolvedValue,
            0.03,
        )
        self.assertEqual(
            target_map["step_etch_03"].parameterProvenance["rateMagnitude"].kind,
            "literal",
        )
        self.assertEqual(target.compileRecord.compilerVersion, "0.0.9")
        self.assertEqual(target.compileRecord.engineVersions, {"cpu": "test-engine"})
        self.assertIn(
            {"name": "rate", "value": 0.1, "source": "override"},
            target.compileRecord.variableSet,
        )
        self.assertIn(
            {"name": "rate", "value": 0.1, "source": "request"},
            target.compileRecord.overrides,
        )
        self.assertIn(
            {
                "stepId": "step_etch_03",
                "parameter": "duration",
                "value": 0.03,
                "source": "measurement-decision",
            },
            target.compileRecord.overrides,
        )

        plain_target = _build_target_compile(store, plain, resolved)
        self.assertEqual(plain_target.compileRecord.variableSet, [
            {
                "name": "step_etch_03.duration",
                "value": 0.03,
                "source": "measurement-decision",
            }
        ])
        self.assertNotEqual(
            target.compileRecord.compileId, plain_target.compileRecord.compileId
        )

        chained = _build_target_compile(store, target, {"step_etch_03": {"duration": 0.05}})
        entries = [
            entry for entry in chained.compileRecord.overrides
            if entry.get("stepId") == "step_etch_03"
            and entry.get("parameter") == "duration"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["value"], 0.05)
        self.assertIn(
            {"name": "rate", "value": 0.1, "source": "request"},
            chained.compileRecord.overrides,
        )
        reused = _build_target_compile(store, varied, resolved)
        self.assertEqual(
            reused.compileRecord.compileId, target.compileRecord.compileId
        )


class MeasurementDecisionMigrationTests(unittest.TestCase):
    def test_v6_database_migrates_to_decisions_schema(self):
        suffix = uuid.uuid4().hex[:6]
        path = storage.VAR / f"test-workflow-decisions-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE measurement_decisions")
            connection.execute(
                "UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'"
            )
        migrated = WorkflowStore(path)
        migrated.initialize()
        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'measurement_decisions'"
            ).fetchone()
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        self.assertIsNotNone(table)
        self.assertEqual(version[0], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
