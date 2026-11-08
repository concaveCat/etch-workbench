"""SQLite schema version management for the workflow store."""

from __future__ import annotations

import sqlite3
import unittest
import uuid

from backend import storage
from backend.workflow.store import SCHEMA_VERSION, WorkflowStore


class WorkflowStoreSchemaTests(unittest.TestCase):
    def test_fresh_store_has_explicit_schema_version(self):
        store = WorkflowStore(storage.VAR / f"test-workflow-fresh-{uuid.uuid4().hex}.sqlite3")
        store.initialize()
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)

    def test_version_one_store_is_migrated_to_current(self):
        path = storage.VAR / f"test-workflow-v1-{uuid.uuid4().hex}.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
        store = WorkflowStore(path)
        store.initialize()
        self.assertEqual(store.schema_version(), SCHEMA_VERSION)

    def test_version_three_store_adds_binding_columns(self):
        path = storage.VAR / f"test-workflow-v3-{uuid.uuid4().hex}.sqlite3"
        store = WorkflowStore(path)
        store.initialize()
        with sqlite3.connect(path) as connection:
            for column in (
                "binding_status",
                "binding_evidence_json",
                "equivalence_status",
                "equivalence_evidence_json",
            ):
                connection.execute(f"ALTER TABLE measurements DROP COLUMN {column}")
            connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        migrated = WorkflowStore(path)
        migrated.initialize()
        self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(measurements)")}
        self.assertIn("binding_status", columns)
        self.assertIn("binding_evidence_json", columns)
        self.assertIn("equivalence_status", columns)
        self.assertIn("equivalence_evidence_json", columns)

    def test_unknown_schema_version_is_rejected(self):
        path = storage.VAR / f"test-workflow-bad-{uuid.uuid4().hex}.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '99')")
        store = WorkflowStore(path)
        with self.assertRaises(RuntimeError):
            store.initialize()
        with sqlite3.connect(path) as connection:
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            version = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0]
        self.assertEqual(tables, ["schema_meta"])
        self.assertEqual(version, "99")


if __name__ == "__main__":
    unittest.main()
