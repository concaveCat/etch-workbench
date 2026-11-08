"""P0-5 ArtifactRef ownership, shared refs, abandon and GC dry-run."""

from __future__ import annotations

import unittest
import uuid

from backend import storage
from backend.workflow.store import WorkflowStore


class WorkflowGcTests(unittest.TestCase):
    def test_abandon_removes_only_one_owner_and_last_release_becomes_candidate(self):
        import os
        from pathlib import Path

        storage.initialize()
        artifact = uuid.uuid4().hex[:24]
        run_dir = storage.RUNS / artifact
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        store = WorkflowStore(Path(storage.VAR) / f"test-gc-{uuid.uuid4().hex}.sqlite3")
        store.initialize()
        store.register_artifact_ref(artifact, "route", "route_a", "continue")
        store.register_artifact_ref(artifact, "route", "route_b", "continue")

        dry = store.gc_dry_run()
        self.assertNotIn(artifact, {item["artifactId"] for item in dry["candidates"]})

        store.abandon_capability(artifact, "route", "route_a", "continue", "owner a released")
        dry = store.gc_dry_run()
        self.assertNotIn(artifact, {item["artifactId"] for item in dry["candidates"]})

        store.abandon_capability(artifact, "route", "route_b", "continue", "owner b released")
        dry = store.gc_dry_run()
        self.assertIn(artifact, {item["artifactId"] for item in dry["candidates"]})
        self.assertEqual(store.list_artifact_refs(artifact), [])

        store.register_artifact_ref(artifact, "route", "route_a", "continue")
        dry = store.gc_dry_run()
        self.assertNotIn(artifact, {item["artifactId"] for item in dry["candidates"]})


if __name__ == "__main__":
    unittest.main()
