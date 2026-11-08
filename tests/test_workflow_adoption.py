"""ProjectV7 adoption into a compiled workflow and first route."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402


def project() -> dict:
    return {
        "schemaVersion": 7,
        "name": "adoption 2D",
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        "steps": [
            {
                "id": "step_etch_01", "name": "etch", "enabled": True, "operation": "etch",
                "model": "isotropic", "rateMagnitude": 0.1, "duration": 0.1, "rateMode": "uniform",
            }
        ],
        "solver": {
            "timeStepRatio": 0.25, "raysPerPoint": 32, "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 7}, "enginePreference": "cpu", "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()

    async def asyncTearDown(self):
        await self.lifespan.__aexit__(None, None, None)

    async def test_adopt_and_create_initial_route(self):
        adopted = await asgi_request(
            "/api/v1/workflow/flows/from-project",
            method="POST",
            payload={"project": project()},
        )
        self.assertEqual(adopted[0], 201, adopted[2])
        body = json_body(adopted)
        self.assertEqual(body["stepCount"], 1)
        self.assertEqual(body["dimension"], 2)
        route = await asgi_request(
            "/api/v1/workflow/routes/from-compile",
            method="POST",
            payload={"compileId": body["compileId"]},
        )
        self.assertEqual(route[0], 201, route[2])
        plan = json_body(route)["plan"]
        self.assertEqual(len(plan["attempts"]), 1)
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["segments"][0]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
