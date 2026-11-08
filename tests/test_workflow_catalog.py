"""Execution catalog API endpoint."""

from __future__ import annotations

import unittest

from tests.test_api_integration import asgi_request, json_body


class ExecutionCatalogApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_lists_all_execution_kinds(self) -> None:
        response = await asgi_request("/api/v1/workflow/execution-catalog")
        self.assertEqual(response[0], 200, response[2])
        payload = json_body(response)
        self.assertEqual(payload["schemaVersion"], 1)
        by_id = {step["id"]: step for step in payload["steps"]}
        self.assertEqual(by_id["isotropic"]["executionKind"], "process")
        self.assertEqual(by_id["implant"]["executionKind"], "field")
        self.assertEqual(by_id["planarize"]["executionKind"], "geometry")
        self.assertGreaterEqual(len(payload["steps"]), 28)


if __name__ == "__main__":
    unittest.main()
