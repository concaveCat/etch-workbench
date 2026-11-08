"""Compatibility migration for pre-solverDigest v2 workflow databases."""

from __future__ import annotations

import json
import sqlite3
import unittest
import uuid

from backend import storage
from backend.workflow.store import SCHEMA_VERSION, WorkflowStore
from backend.workflow.verification import verify_route_plan_compiles
from tests.workflow_fixtures import compiled_base, compiled_branch, compiled_with_solver, plan_from_compiled
from tests.test_workflow_adoption import project as project_fixture


def downgrade_to_v2(path, *, drop_compiles: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        for table, column in (
            ("attempts", "prefix_compatibility_json"),
            ("reuse", "compatibility_json"),
        ):
            rows = connection.execute(f"SELECT rowid, {column} FROM {table}").fetchall()
            for rowid, raw in rows:
                if raw is None:
                    continue
                payload = json.loads(raw)
                payload.pop("solverDigest", None)
                connection.execute(
                    f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                    (json.dumps(payload, sort_keys=True, separators=(",", ":")), rowid),
                )
        for table in ("attempts", "reuse"):
            try:
                connection.execute(f"ALTER TABLE {table} DROP COLUMN legacy_compatibility_json")
            except sqlite3.OperationalError:
                pass
        try:
            connection.execute("ALTER TABLE routes DROP COLUMN legacy_unverified")
        except sqlite3.OperationalError:
            pass
        if drop_compiles:
            connection.execute("DELETE FROM compiles")
        connection.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")


class WorkflowMigrationTests(unittest.TestCase):
    def _store_with_plan(self) -> tuple[WorkflowStore, str, str]:
        suffix = uuid.uuid4().hex[:6]
        base = compiled_base()
        branch = compiled_branch()
        plan = plan_from_compiled(base, branch, suffix=suffix)
        path = storage.VAR / f"test-workflow-migrate-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        store.register_compile(base)
        store.register_compile(branch)
        store.create_plan(plan)
        return store, path, plan.route.routeId

    def test_v2_route_is_repaired_from_saved_compiles(self):
        store, path, route_id = self._store_with_plan()
        downgrade_to_v2(path)
        migrated = WorkflowStore(path)
        migrated.initialize()
        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        plan = migrated.get_plan(route_id)
        self.assertFalse(plan.route.legacyUnverified)
        self.assertIsNotNone(plan.attempts[1].prefixCompatibility)
        self.assertNotEqual(plan.attempts[1].prefixCompatibility.solverDigest, "0" * 64)
        self.assertEqual(
            plan.attempts[1].prefixCompatibility.solverDigest,
            plan.reuse[0].compatibility.solverDigest,
        )
        self.assertIsNone(plan.attempts[1].legacyCompatibility)

    def test_v2_route_with_incompatible_source_and_target_solvers_becomes_legacy(self):
        suffix = uuid.uuid4().hex[:6]
        base = compiled_base()
        changed = compiled_with_solver()
        plan = plan_from_compiled(base, base, suffix=suffix)
        path = storage.VAR / f"test-workflow-migrate-solver-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        store.register_compile(base)
        store.register_compile(changed)
        store.create_plan(plan)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE attempts SET compile_id = ?, flow_digest = ? WHERE attempt_id = ?",
                (changed.compileRecord.compileId, changed.compileRecord.fullProjectHash, plan.attempts[1].attemptId),
            )
            connection.execute(
                "UPDATE segments SET source_compile_id = ?, flow_digest = ? WHERE segment_id = ?",
                (changed.compileRecord.compileId, changed.compileRecord.fullProjectHash, plan.segments[2].segmentId),
            )
        downgrade_to_v2(path)
        migrated = WorkflowStore(path)
        migrated.initialize()
        migrated_plan = migrated.get_plan(plan.route.routeId)
        self.assertTrue(migrated_plan.route.legacyUnverified)
        self.assertIsNotNone(migrated_plan.attempts[1].legacyCompatibility)
        self.assertIsNotNone(migrated_plan.reuse[0].legacyCompatibility)
        with self.assertRaises(ValueError):
            verify_route_plan_compiles(
                migrated_plan,
                {base.compileRecord.compileId: base, changed.compileRecord.compileId: changed},
            )

    def test_v2_route_without_compiles_loads_as_unverified(self):
        store, path, route_id = self._store_with_plan()
        downgrade_to_v2(path, drop_compiles=True)
        migrated = WorkflowStore(path)
        migrated.initialize()
        plan = migrated.get_plan(route_id)
        self.assertTrue(plan.route.legacyUnverified)
        self.assertIsNotNone(plan.attempts[1].prefixCompatibility)
        self.assertEqual(plan.attempts[1].prefixCompatibility.solverDigest, "0" * 64)
        self.assertIsNotNone(plan.attempts[1].legacyCompatibility)
        self.assertIsNotNone(plan.reuse[0].legacyCompatibility)

    def test_v11_database_gains_the_reports_table(self):
        suffix = uuid.uuid4().hex[:6]
        path = storage.VAR / f"test-workflow-migrate-reports-{suffix}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE workflow_reports")
            connection.execute("UPDATE schema_meta SET value = '11' WHERE key = 'schema_version'")
        migrated = WorkflowStore(path)
        migrated.initialize()
        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("workflow_reports", tables)
        record = migrated.create_project({**project_fixture(), "name": f"迁移报告 {suffix}"})[0]
        saved = migrated.save_report(
            record["projectId"], "report_migrated", "迁移报告", "<html></html>", "0" * 64
        )
        self.assertEqual(saved["reportId"], "report_migrated")
        self.assertEqual(migrated.get_latest_report(record["projectId"])["title"], "迁移报告")


if __name__ == "__main__":
    unittest.main()
