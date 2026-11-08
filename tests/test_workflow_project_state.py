"""Per-project 03-page workflow state (schema 11).

The state is a pointer document: it remembers which compile/route/segments/
measurements/experiments belong to a project without copying their content.
These tests cover the store contract, the reference checks, the schema
migration, and the typed HTTP endpoints.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.workflow.api import get_store  # noqa: E402
from backend.workflow.metrology import MeasurementResultV1  # noqa: E402
from backend.workflow.project_state import (  # noqa: E402
    MAX_COMPILE_BASIS_CHARS,
    MAX_EXPERIMENT_IDS,
    MAX_MEASUREMENT_IDS,
    MAX_SEGMENT_RUNS,
    ProjectWorkflowSegmentRunV1,
    ProjectWorkflowStateV1,
)
from backend.workflow.store import (  # noqa: E402
    SCHEMA_VERSION,
    WorkflowNotFound,
    WorkflowStore,
    WorkflowValidation,
)
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_projects import project  # noqa: E402
from tests.workflow_fixtures import (  # noqa: E402
    compiled_base,
    compiled_branch,
    compiled_with_solver,
    plan_from_compiled,
)

PROJECTS = "/api/v1/workflow/projects"


def measurement(measurement_id: str | None = None) -> MeasurementResultV1:
    return MeasurementResultV1(
        schemaVersion=1,
        measurementId=measurement_id or f"m_{uuid.uuid4().hex}",
        metricId="metric_depth",
        recipeId="recipe_depth",
        recipeVersion=1,
        method="cd_at_height",
        algorithmVersion="v1",
        sourceSnapshotHash="a" * 64,
        status="measured",
        value=0.1,
        unit="um",
        material="Si",
    )


class ProjectStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.root = storage.VAR / f"test-project-state-{suffix}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = WorkflowStore(self.root / "workflow.sqlite3")
        self.store.initialize(seed=False)

        self.project, _ = self.store.create_project(project(f"state {suffix}"))
        self.project_id = self.project["projectId"]

        self.base = compiled_base()
        self.branch = compiled_branch()
        self.store.register_compile(self.base)
        self.store.register_compile(self.branch)
        self.plan = plan_from_compiled(self.base, self.branch, suffix=suffix)
        self.store.create_plan(self.plan)

        self.measurement = self.store.record_measurement(measurement())
        self.experiment_id = f"exp_{uuid.uuid4().hex[:16]}"
        self.store.save_experiment(
            self.experiment_id,
            {"experimentId": self.experiment_id, "name": "state experiment"},
        )

    def state(self, **overrides) -> dict:
        segment = self.plan.segments[0]
        payload = {
            "schemaVersion": 1,
            "compileId": self.base.compileRecord.compileId,
            "compileBasis": '{"dimension":2,"steps":[]}',
            "routeId": self.plan.route.routeId,
            "segmentRuns": [
                {
                    "segmentId": segment.segmentId,
                    "runId": segment.runId,
                    "measurementId": self.measurement.measurementId,
                }
            ],
            "measurementIds": [self.measurement.measurementId],
            "experimentIds": [self.experiment_id],
        }
        payload.update(overrides)
        return payload

    def test_unset_state_reads_as_an_empty_document(self) -> None:
        state = self.store.get_project_workflow_state(self.project_id)
        self.assertEqual(state.compileId, None)
        self.assertIsNone(state.compileBasis)
        self.assertEqual(state.routeId, None)
        self.assertEqual(state.segmentRuns, [])
        self.assertEqual(state.measurementIds, [])
        self.assertEqual(state.experimentIds, [])
        self.assertIsNone(state.updatedAt)

    def test_save_and_read_round_trip(self) -> None:
        saved = self.store.save_project_workflow_state(self.project_id, self.state())
        self.assertIsNotNone(saved.updatedAt)
        self.assertEqual(saved.compileId, self.base.compileRecord.compileId)
        self.assertEqual(saved.routeId, self.plan.route.routeId)
        self.assertEqual(
            [entry.segmentId for entry in saved.segmentRuns],
            [self.plan.segments[0].segmentId],
        )

        loaded = self.store.get_project_workflow_state(self.project_id)
        self.assertEqual(loaded, saved)
        with sqlite3.connect(self.store.path) as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM project_workflow_state WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        self.assertEqual(count, 1)

    def test_compile_basis_is_stored_verbatim(self) -> None:
        basis = '{"dimension":2,"domain":{"xExtent":4},"steps":[{"id":"0.01","duration":0.01}]}'
        saved = self.store.save_project_workflow_state(
            self.project_id, self.state(compileBasis=basis)
        )
        self.assertEqual(saved.compileBasis, basis)
        self.assertEqual(saved.content()["compileBasis"], basis)
        loaded = self.store.get_project_workflow_state(self.project_id)
        self.assertEqual(loaded.compileBasis, basis)
        with sqlite3.connect(self.store.path) as connection:
            (raw,) = connection.execute(
                "SELECT state_json FROM project_workflow_state WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        self.assertEqual(json.loads(raw)["compileBasis"], basis)

    def test_legacy_state_without_a_compile_basis_reads_as_null(self) -> None:
        # Schema 12 states were written before the basis existed.
        self.store.save_project_workflow_state(self.project_id, self.state())
        with sqlite3.connect(self.store.path) as connection:
            (raw,) = connection.execute(
                "SELECT state_json FROM project_workflow_state WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        legacy = json.loads(raw)
        legacy.pop("compileBasis", None)
        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "UPDATE project_workflow_state SET state_json = ? WHERE project_id = ?",
                (json.dumps(legacy), self.project_id),
            )
        loaded = self.store.get_project_workflow_state(self.project_id)
        self.assertIsNone(loaded.compileBasis)
        self.assertEqual(loaded.compileId, self.base.compileRecord.compileId)

    def test_compile_basis_length_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            ProjectWorkflowStateV1(compileBasis="x" * (MAX_COMPILE_BASIS_CHARS + 1))
        accepted = ProjectWorkflowStateV1(compileBasis="x" * MAX_COMPILE_BASIS_CHARS)
        self.assertEqual(len(accepted.compileBasis or ""), MAX_COMPILE_BASIS_CHARS)
        with self.assertRaises(ValueError):
            self.store.save_project_workflow_state(
                self.project_id,
                self.state(compileBasis="x" * (MAX_COMPILE_BASIS_CHARS + 1)),
            )

    def test_repeated_identical_put_keeps_updated_at(self) -> None:
        first = self.store.save_project_workflow_state(self.project_id, self.state())
        # Pin the stamp to the past so a rewrite would be visible even though
        # _now() has one-second resolution.
        frozen = "2020-01-01T00:00:00+00:00"
        document = {**first.content(), "updatedAt": frozen}
        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "UPDATE project_workflow_state SET state_json = ?, updated_at = ?"
                " WHERE project_id = ?",
                (json.dumps(document, sort_keys=True), frozen, self.project_id),
            )

        repeated = self.store.save_project_workflow_state(self.project_id, self.state())
        self.assertEqual(repeated.updatedAt, frozen)
        with sqlite3.connect(self.store.path) as connection:
            row = connection.execute(
                "SELECT state_json, updated_at FROM project_workflow_state"
                " WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        self.assertEqual(row[1], frozen)
        self.assertEqual(json.loads(row[0])["updatedAt"], frozen)

    def test_changed_content_moves_updated_at(self) -> None:
        first = self.store.save_project_workflow_state(self.project_id, self.state())
        frozen = "2020-01-01T00:00:00+00:00"
        document = {**first.content(), "updatedAt": frozen}
        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "UPDATE project_workflow_state SET state_json = ?, updated_at = ?"
                " WHERE project_id = ?",
                (json.dumps(document, sort_keys=True), frozen, self.project_id),
            )

        changed = self.store.save_project_workflow_state(
            self.project_id, self.state(experimentIds=[])
        )
        self.assertNotEqual(changed.updatedAt, frozen)
        self.assertEqual(changed.experimentIds, [])

    def test_explicit_clear_is_allowed(self) -> None:
        self.store.save_project_workflow_state(self.project_id, self.state())
        cleared = self.store.save_project_workflow_state(
            self.project_id,
            {
                "schemaVersion": 1,
                "compileId": None,
                "routeId": None,
                "segmentRuns": [],
                "measurementIds": [],
                "experimentIds": [],
            },
        )
        self.assertIsNone(cleared.compileId)
        self.assertIsNone(cleared.routeId)
        self.assertEqual(cleared.segmentRuns, [])
        self.assertIsNotNone(cleared.updatedAt)

    def test_dangling_references_are_rejected_with_the_offending_id(self) -> None:
        missing_compile = f"c_{'0' * 24}"
        missing_route = f"route_{uuid.uuid4().hex[:16]}"
        missing_segment = f"segment_{uuid.uuid4().hex[:16]}"
        missing_measurement = f"m_{uuid.uuid4().hex}"
        missing_experiment = f"exp_{uuid.uuid4().hex[:16]}"
        cases = (
            ({"compileId": missing_compile}, missing_compile),
            ({"routeId": missing_route}, missing_route),
            (
                {
                    "segmentRuns": [
                        {
                            "segmentId": missing_segment,
                            "runId": "run_missing",
                        }
                    ]
                },
                missing_segment,
            ),
            (
                {
                    "segmentRuns": [
                        {
                            "segmentId": self.plan.segments[0].segmentId,
                            "runId": self.plan.segments[0].runId,
                            "measurementId": missing_measurement,
                        }
                    ]
                },
                missing_measurement,
            ),
            ({"measurementIds": [missing_measurement]}, missing_measurement),
            ({"experimentIds": [missing_experiment]}, missing_experiment),
        )
        for overrides, expected_id in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(WorkflowValidation) as caught:
                    self.store.save_project_workflow_state(
                        self.project_id, self.state(**overrides)
                    )
                self.assertIn(expected_id, str(caught.exception))
        # Nothing was written for the rejected documents.
        self.assertIsNone(
            self.store.get_project_workflow_state(self.project_id).updatedAt
        )

    def test_route_must_belong_to_the_compile(self) -> None:
        other = compiled_with_solver()
        self.store.register_compile(other)
        with self.assertRaises(WorkflowValidation) as caught:
            self.store.save_project_workflow_state(
                self.project_id,
                self.state(compileId=other.compileRecord.compileId),
            )
        message = str(caught.exception)
        self.assertIn(self.plan.route.routeId, message)
        self.assertIn(other.compileRecord.compileId, message)
        self.assertIn("does not belong", message)

    def test_segment_must_belong_to_the_route(self) -> None:
        # Build a second route from the same compiles and point the state at
        # that route while keeping this route's segment.
        other_plan = plan_from_compiled(
            self.base, self.branch, suffix=f"x{uuid.uuid4().hex[:7]}"
        )
        self.store.create_plan(other_plan)
        with self.assertRaises(WorkflowValidation) as caught:
            self.store.save_project_workflow_state(
                self.project_id,
                self.state(routeId=other_plan.route.routeId),
            )
        message = str(caught.exception)
        self.assertIn(self.plan.segments[0].segmentId, message)
        self.assertIn(other_plan.route.routeId, message)

    def test_segment_runs_require_a_route(self) -> None:
        with self.assertRaises(WorkflowValidation) as caught:
            self.store.save_project_workflow_state(
                self.project_id, self.state(routeId=None)
            )
        self.assertIn("routeId", str(caught.exception))

    def test_get_returns_saved_state_after_a_reference_is_gone(self) -> None:
        saved = self.store.save_project_workflow_state(self.project_id, self.state())
        with sqlite3.connect(self.store.path) as connection:
            connection.execute(
                "DELETE FROM measurements WHERE measurement_id = ?",
                (self.measurement.measurementId,),
            )
        loaded = self.store.get_project_workflow_state(self.project_id)
        self.assertEqual(loaded, saved)
        self.assertEqual(loaded.measurementIds, [self.measurement.measurementId])

    def test_missing_project_is_404(self) -> None:
        missing = f"proj_{uuid.uuid4().hex[:24]}"
        with self.assertRaises(WorkflowNotFound):
            self.store.get_project_workflow_state(missing)
        with self.assertRaises(WorkflowNotFound):
            self.store.save_project_workflow_state(missing, self.state())
        with self.assertRaises(WorkflowNotFound):
            self.store.delete_project_workflow_state(missing)

    def test_delete_project_cascades_the_state(self) -> None:
        self.store.save_project_workflow_state(self.project_id, self.state())
        self.store.delete_project(self.project_id)
        with sqlite3.connect(self.store.path) as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM project_workflow_state WHERE project_id = ?",
                (self.project_id,),
            ).fetchone()
        self.assertEqual(count, 0)
        with self.assertRaises(WorkflowNotFound):
            self.store.get_project_workflow_state(self.project_id)

    def test_delete_state_keeps_the_project(self) -> None:
        self.store.save_project_workflow_state(self.project_id, self.state())
        self.store.delete_project_workflow_state(self.project_id)
        state = self.store.get_project_workflow_state(self.project_id)
        self.assertIsNone(state.updatedAt)
        self.assertIsNotNone(self.store.get_project(self.project_id))

    def test_limits_are_enforced(self) -> None:
        too_many_runs = [
            ProjectWorkflowSegmentRunV1(segmentId=f"S{i:04d}", runId=f"run{i:04d}")
            for i in range(MAX_SEGMENT_RUNS + 1)
        ]
        with self.assertRaises(ValueError):
            ProjectWorkflowStateV1(segmentRuns=too_many_runs)
        with self.assertRaises(ValueError):
            ProjectWorkflowStateV1(
                measurementIds=[f"m_{i:04d}" for i in range(MAX_MEASUREMENT_IDS + 1)]
            )
        with self.assertRaises(ValueError):
            ProjectWorkflowStateV1(
                experimentIds=[f"exp_{i:04d}" for i in range(MAX_EXPERIMENT_IDS + 1)]
            )
        # Exactly at the limit is accepted.
        ProjectWorkflowStateV1(
            segmentRuns=[
                ProjectWorkflowSegmentRunV1(segmentId=f"S{i:04d}", runId=f"run{i:04d}")
                for i in range(MAX_SEGMENT_RUNS)
            ],
            measurementIds=[f"m_{i:04d}" for i in range(MAX_MEASUREMENT_IDS)],
            experimentIds=[f"exp_{i:04d}" for i in range(MAX_EXPERIMENT_IDS)],
        )

    def test_state_writes_do_not_compile_or_route(self) -> None:
        with sqlite3.connect(self.store.path) as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("compiles", "routes", "attempts", "segments")
            }
        self.store.save_project_workflow_state(self.project_id, self.state())
        self.store.get_project_workflow_state(self.project_id)
        with sqlite3.connect(self.store.path) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("compiles", "routes", "attempts", "segments")
            }
        self.assertEqual(after, before)


class ProjectStateMigrationTests(unittest.TestCase):
    def test_schema_10_database_gains_the_state_table(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        path = storage.VAR / f"test-project-state-migrate-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize(seed=False)
        record, _ = store.create_project(project(f"migrate state {suffix}"))
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE project_workflow_state")
            connection.execute(
                "UPDATE schema_meta SET value = '10' WHERE key = 'schema_version'"
            )
        self.assertNotEqual(SCHEMA_VERSION, "10")

        upgraded = WorkflowStore(path)
        upgraded.initialize(seed=False)
        self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("project_workflow_state", tables)
        # Old project rows survive the migration.
        self.assertEqual(upgraded.get_project(record["projectId"])["name"], record["name"])
        saved = upgraded.save_project_workflow_state(
            record["projectId"],
            {
                "schemaVersion": 1,
                "compileId": None,
                "routeId": None,
                "segmentRuns": [],
                "measurementIds": [],
                "experimentIds": [],
            },
        )
        self.assertIsNotNone(saved.updatedAt)

    def test_schema_12_database_keeps_its_state_and_gains_the_basis(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        path = storage.VAR / f"test-project-state-basis-migrate-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize(seed=False)
        record, _ = store.create_project(project(f"migrate basis {suffix}"))
        store.save_project_workflow_state(
            record["projectId"],
            {
                "schemaVersion": 1,
                "compileId": None,
                "routeId": None,
                "segmentRuns": [],
                "measurementIds": [],
                "experimentIds": [],
            },
        )
        # A schema-12 row predates compileBasis: strip the field and stamp the
        # old version. The 12 → 13 migration only updates schema_meta.
        with sqlite3.connect(path) as connection:
            (raw,) = connection.execute(
                "SELECT state_json FROM project_workflow_state WHERE project_id = ?",
                (record["projectId"],),
            ).fetchone()
            document = json.loads(raw)
            document.pop("compileBasis", None)
            connection.execute(
                "UPDATE project_workflow_state SET state_json = ? WHERE project_id = ?",
                (json.dumps(document), record["projectId"]),
            )
            connection.execute(
                "UPDATE schema_meta SET value = '12' WHERE key = 'schema_version'"
            )
        self.assertNotEqual(SCHEMA_VERSION, "12")

        upgraded = WorkflowStore(path)
        upgraded.initialize(seed=False)
        self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("project_workflow_state", tables)
        loaded = upgraded.get_project_workflow_state(record["projectId"])
        self.assertIsNone(loaded.compileBasis)
        saved = upgraded.save_project_workflow_state(
            record["projectId"],
            {
                "schemaVersion": 1,
                "compileId": None,
                "compileBasis": "basis-after-migration",
                "routeId": None,
                "segmentRuns": [],
                "measurementIds": [],
                "experimentIds": [],
            },
        )
        self.assertEqual(saved.compileBasis, "basis-after-migration")
        self.assertEqual(
            upgraded.get_project_workflow_state(record["projectId"]).compileBasis,
            "basis-after-migration",
        )


class ProjectStateApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:8]
        self.store = get_store()
        self.base = compiled_base()
        self.branch = compiled_branch()
        self.store.register_compile(self.base)
        self.store.register_compile(self.branch)
        self.plan = plan_from_compiled(
            self.base, self.branch, suffix=f"api{self.suffix}"
        )
        self.store.create_plan(self.plan)
        self.measurement = self.store.record_measurement(measurement())
        self.experiment_id = f"exp_{uuid.uuid4().hex[:16]}"
        self.store.save_experiment(
            self.experiment_id,
            {"experimentId": self.experiment_id, "name": "api state"},
        )

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def post(self, path: str, payload: dict):
        return await asgi_request(path, method="POST", payload=payload)

    async def get(self, path: str):
        return await asgi_request(path, method="GET")

    async def put(self, path: str, payload: dict):
        return await asgi_request(path, method="PUT", payload=payload)

    async def delete(self, path: str):
        return await asgi_request(path, method="DELETE")

    async def new_project(self) -> str:
        created = await self.post(PROJECTS, project(f"api state {self.suffix}"))
        self.assertEqual(created[0], 201, created[2])
        return json_body(created)["projectId"]

    def state(self, **overrides) -> dict:
        segment = self.plan.segments[0]
        payload = {
            "schemaVersion": 1,
            "compileId": self.base.compileRecord.compileId,
            "compileBasis": '{"dimension":2,"steps":[]}',
            "routeId": self.plan.route.routeId,
            "segmentRuns": [
                {
                    "segmentId": segment.segmentId,
                    "runId": segment.runId,
                    "measurementId": self.measurement.measurementId,
                }
            ],
            "measurementIds": [self.measurement.measurementId],
            "experimentIds": [self.experiment_id],
        }
        payload.update(overrides)
        return payload

    async def test_unset_state_is_200_empty(self) -> None:
        project_id = await self.new_project()
        response = await self.get(f"{PROJECTS}/{project_id}/state")
        self.assertEqual(response[0], 200, response[2])
        body = json_body(response)
        self.assertEqual(body["schemaVersion"], 1)
        self.assertIsNone(body["compileId"])
        self.assertIsNone(body["compileBasis"])
        self.assertIsNone(body["routeId"])
        self.assertEqual(body["segmentRuns"], [])
        self.assertEqual(body["measurementIds"], [])
        self.assertEqual(body["experimentIds"], [])
        self.assertIsNone(body["updatedAt"])

    async def test_put_get_round_trip_and_idempotent_stamp(self) -> None:
        project_id = await self.new_project()
        first = await self.put(f"{PROJECTS}/{project_id}/state", self.state())
        self.assertEqual(first[0], 200, first[2])
        saved = json_body(first)
        self.assertEqual(saved["compileId"], self.base.compileRecord.compileId)
        self.assertEqual(saved["compileBasis"], '{"dimension":2,"steps":[]}')
        self.assertEqual(saved["routeId"], self.plan.route.routeId)
        self.assertIsNotNone(saved["updatedAt"])

        fetched = await self.get(f"{PROJECTS}/{project_id}/state")
        self.assertEqual(fetched[0], 200, fetched[2])
        self.assertEqual(json_body(fetched), saved)

        repeated = await self.put(f"{PROJECTS}/{project_id}/state", self.state())
        self.assertEqual(repeated[0], 200, repeated[2])
        self.assertEqual(json_body(repeated)["updatedAt"], saved["updatedAt"])

    async def test_compile_basis_round_trip_and_clearing(self) -> None:
        project_id = await self.new_project()
        basis = "sha256:" + "a" * 64
        saved = json_body(await self.put(
            f"{PROJECTS}/{project_id}/state", self.state(compileBasis=basis)
        ))
        self.assertEqual(saved["compileBasis"], basis)
        # Omitting the field clears it again: null is a legal value.
        cleared = json_body(await self.put(
            f"{PROJECTS}/{project_id}/state", self.state(compileBasis=None)
        ))
        self.assertIsNone(cleared["compileBasis"])
        too_long = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(compileBasis="x" * (MAX_COMPILE_BASIS_CHARS + 1)),
        )
        self.assertEqual(too_long[0], 422, too_long[2])

    async def test_dangling_reference_is_422_with_the_id(self) -> None:
        project_id = await self.new_project()
        missing = f"m_{uuid.uuid4().hex}"
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(measurementIds=[missing]),
        )
        self.assertEqual(response[0], 422, response[2])
        self.assertIn(missing, json_body(response)["detail"])

        missing_compile = f"c_{'0' * 24}"
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(compileId=missing_compile),
        )
        self.assertEqual(response[0], 422, response[2])
        self.assertIn(missing_compile, json_body(response)["detail"])

    async def test_route_not_in_compile_is_422(self) -> None:
        project_id = await self.new_project()
        other = compiled_with_solver()
        self.store.register_compile(other)
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(compileId=other.compileRecord.compileId),
        )
        self.assertEqual(response[0], 422, response[2])
        self.assertIn("does not belong", json_body(response)["detail"])

    async def test_missing_project_is_404(self) -> None:
        missing = f"proj_{uuid.uuid4().hex[:24]}"
        self.assertEqual((await self.get(f"{PROJECTS}/{missing}/state"))[0], 404)
        self.assertEqual(
            (await self.put(f"{PROJECTS}/{missing}/state", self.state()))[0], 404
        )

    async def test_delete_project_removes_the_state(self) -> None:
        project_id = await self.new_project()
        self.assertEqual(
            (await self.put(f"{PROJECTS}/{project_id}/state", self.state()))[0], 200
        )
        self.assertEqual((await self.delete(f"{PROJECTS}/{project_id}"))[0], 204)
        # The project is gone, so its state endpoint is 404 too.
        self.assertEqual((await self.get(f"{PROJECTS}/{project_id}/state"))[0], 404)
        with sqlite3.connect(self.store.path) as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM project_workflow_state WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        self.assertEqual(count, 0)

    async def test_delete_state_endpoint_clears_but_keeps_project(self) -> None:
        project_id = await self.new_project()
        await self.put(f"{PROJECTS}/{project_id}/state", self.state())
        cleared = await self.delete(f"{PROJECTS}/{project_id}/state")
        self.assertEqual(cleared[0], 204, cleared[2])
        body = json_body(await self.get(f"{PROJECTS}/{project_id}/state"))
        self.assertIsNone(body["updatedAt"])
        self.assertIsNone(body["compileId"])
        self.assertEqual((await self.get(f"{PROJECTS}/{project_id}"))[0], 200)

    async def test_over_limit_is_422(self) -> None:
        project_id = await self.new_project()
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(
                segmentRuns=[
                    {"segmentId": f"S{i:04d}", "runId": f"run{i:04d}"}
                    for i in range(MAX_SEGMENT_RUNS + 1)
                ]
            ),
        )
        self.assertEqual(response[0], 422, response[2])
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(
                measurementIds=[f"m_{i:04d}" for i in range(MAX_MEASUREMENT_IDS + 1)]
            ),
        )
        self.assertEqual(response[0], 422, response[2])
        response = await self.put(
            f"{PROJECTS}/{project_id}/state",
            self.state(
                experimentIds=[f"exp_{i:04d}" for i in range(MAX_EXPERIMENT_IDS + 1)]
            ),
        )
        self.assertEqual(response[0], 422, response[2])

    async def test_state_write_creates_no_compile_or_route(self) -> None:
        project_id = await self.new_project()
        before_compiles = json_body(await self.get("/api/v1/workflow/compiles"))["compiles"]
        before_routes = json_body(await self.get("/api/v1/workflow/routes"))["routes"]
        self.assertEqual(
            (await self.put(f"{PROJECTS}/{project_id}/state", self.state()))[0], 200
        )
        after_compiles = json_body(await self.get("/api/v1/workflow/compiles"))["compiles"]
        after_routes = json_body(await self.get("/api/v1/workflow/routes"))["routes"]
        self.assertEqual(len(after_compiles), len(before_compiles))
        self.assertEqual(len(after_routes), len(before_routes))


if __name__ == "__main__":
    unittest.main()
