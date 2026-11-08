"""Persisted 06 reports (schema 12): save, latest, isolation and limits."""

from __future__ import annotations

import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from backend.workflow.api import get_store  # noqa: E402
from backend.workflow.store import (  # noqa: E402
    MAX_REPORT_HTML_BYTES,
    MAX_REPORTS_PER_PROJECT,
    SCHEMA_VERSION,
    WorkflowValidation,
)
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_adoption import project  # noqa: E402


class ReportPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        suffix = uuid.uuid4().hex[:6]
        self.suffix = suffix
        self.store = get_store()
        self.assertEqual(self.store.schema_version(), SCHEMA_VERSION)
        self.project_ids = []
        for name in (f"报告工程 A {suffix}", f"报告工程 B {suffix}"):
            document = project()
            document["name"] = name
            response = await asgi_request("/api/v1/workflow/projects", method="POST", payload=document)
            self.assertEqual(response[0], 201, response[2])
            self.project_ids.append(json_body(response)["projectId"])
        self.experiment_id = f"exp_reports_{suffix}"
        self.store.save_experiment(self.experiment_id, {
            "schemaVersion": 2,
            "kind": "manual",
            "experimentId": self.experiment_id,
            "name": "报告来源实验",
            "status": "planned",
            "variants": [],
        })

    async def asyncTearDown(self):
        for project_id in self.project_ids:
            try:
                self.store.delete_project(project_id)
            except Exception:
                pass
        try:
            self.store.delete_experiment(self.experiment_id)
        except Exception:
            pass
        await self.lifespan.__aexit__(None, None, None)

    async def _generate(self, project_id: str, title: str) -> dict:
        response = await asgi_request(
            "/api/v1/workflow/analysis/report",
            method="POST",
            payload={
                "title": title,
                "projectId": project_id,
                "experimentId": self.experiment_id,
                "conclusions": "报告结论",
            },
        )
        self.assertEqual(response[0], 200, response[2])
        return json_body(response)

    async def test_report_is_stored_and_restored_as_latest(self):
        project_a = self.project_ids[0]
        empty = await asgi_request(f"/api/v1/workflow/projects/{project_a}/reports/latest")
        self.assertEqual(empty[0], 200, empty[2])
        self.assertIsNone(json_body(empty))

        report = await self._generate(project_a, "持久化报告")
        self.assertRegex(report["reportId"], r"^report_[a-f0-9]{16}$")

        latest = await asgi_request(f"/api/v1/workflow/projects/{project_a}/reports/latest")
        self.assertEqual(latest[0], 200, latest[2])
        restored = json_body(latest)
        self.assertEqual(restored["reportId"], report["reportId"])
        self.assertEqual(restored["title"], "持久化报告")
        self.assertEqual(restored["sha256"], report["sha256"])
        self.assertEqual(restored["html"], report["html"])

        single = await asgi_request(
            f"/api/v1/workflow/projects/{project_a}/reports/{report['reportId']}"
        )
        self.assertEqual(single[0], 200, single[2])
        self.assertEqual(json_body(single)["html"], report["html"])

        # A second project must not see the first project's report.
        other = await asgi_request(
            f"/api/v1/workflow/projects/{self.project_ids[1]}/reports/latest"
        )
        self.assertEqual(other[0], 200, other[2])
        self.assertIsNone(json_body(other))
        missing = await asgi_request(
            f"/api/v1/workflow/projects/{self.project_ids[1]}/reports/{report['reportId']}"
        )
        self.assertEqual(missing[0], 404, missing[2])

    async def test_delete_report_and_project_cascade(self):
        project_a = self.project_ids[0]
        report = await self._generate(project_a, "可删除报告")
        deleted = await asgi_request(
            f"/api/v1/workflow/projects/{project_a}/reports/{report['reportId']}",
            method="DELETE",
        )
        self.assertEqual(deleted[0], 204, deleted[2])
        again = await asgi_request(
            f"/api/v1/workflow/projects/{project_a}/reports/{report['reportId']}",
            method="DELETE",
        )
        self.assertEqual(again[0], 404, again[2])

        report = await self._generate(project_a, "级联删除报告")
        removed = await asgi_request(
            f"/api/v1/workflow/projects/{project_a}", method="DELETE"
        )
        self.assertEqual(removed[0], 204, removed[2])
        gone = await asgi_request(f"/api/v1/workflow/projects/{project_a}/reports/latest")
        self.assertEqual(gone[0], 404, gone[2])
        with sqlite3.connect(self.store.path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM workflow_reports WHERE project_id = ?",
                (project_a,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_report_for_unknown_project_is_rejected(self):
        response = await asgi_request(
            "/api/v1/workflow/analysis/report",
            method="POST",
            payload={
                "title": "无主报告",
                "projectId": "proj_000000000000000000000000",
                "experimentId": self.experiment_id,
            },
        )
        self.assertEqual(response[0], 404, response[2])

    def test_html_size_limit_and_oldest_replacement(self):
        project_id = self.project_ids[0]
        too_large = "x" * (MAX_REPORT_HTML_BYTES + 1)
        with self.assertRaises(WorkflowValidation):
            self.store.save_report(project_id, "report_too_large", "太大", too_large, "0" * 64)

        for index in range(MAX_REPORTS_PER_PROJECT + 5):
            self.store.save_report(
                project_id,
                f"report_{index:04d}",
                f"报告 {index}",
                f"<html><body>{index}</body></html>",
                f"{index:064d}",
                f"2026-10-31T00:00:{index:02d}+00:00",
            )
        with sqlite3.connect(self.store.path) as connection:
            rows = connection.execute(
                "SELECT report_id FROM workflow_reports WHERE project_id = ?"
                " ORDER BY created_at ASC",
                (project_id,),
            ).fetchall()
        self.assertEqual(len(rows), MAX_REPORTS_PER_PROJECT)
        kept = {row[0] for row in rows}
        self.assertNotIn("report_0000", kept)
        self.assertNotIn("report_0004", kept)
        self.assertIn(f"report_{MAX_REPORTS_PER_PROJECT + 4:04d}", kept)
        latest = self.store.get_latest_report(project_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["reportId"], f"report_{MAX_REPORTS_PER_PROJECT + 4:04d}")


if __name__ == "__main__":
    unittest.main()
