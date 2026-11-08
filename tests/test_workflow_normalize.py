"""POST /projects/normalize: the canonical, default-completed project form.

The 03 page stores a fingerprint of the authored project next to its Compile.
Saving a project goes through ``ProjectV7`` and therefore fills defaults the
imported JSON omits (``maskMaterial``, Fin ``taperAngle``/``maskHeight``,
``stack.baseHeight``, ``step.rateMode``, solver defaults …). The normalize
endpoint exposes exactly that canonical form — without persisting anything and
without compiling — so the client can fingerprint the document the server
would store.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
import uuid

os.environ.setdefault("WORKBENCH_PORT", "8767")

from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402
from tests.test_workflow_projects import etch_step, project  # noqa: E402

NORMALIZE = "/api/v1/workflow/projects/normalize"
PROJECTS = "/api/v1/workflow/projects"


def _base(name: str) -> dict:
    """A minimal schema-7 project that only states the required fields."""

    return {
        "schemaVersion": 7,
        "name": name,
        "dimension": 2,
        "units": {"length": "um", "time": "s", "rate": "um/s", "angle": "deg"},
        "domain": {"gridDelta": 0.2, "xExtent": 4.0, "xBoundary": "reflective"},
        "geometry": {"kind": "plane", "height": 0.0, "substrateMaterial": "Si"},
        # rateMode is deliberately omitted: the server default is "uniform".
        "steps": [{
            "id": "step_norm_01",
            "name": "noop",
            "enabled": True,
            "operation": "etch",
            "model": "isotropic",
            "rateMagnitude": 0.0,
            "duration": 0.01,
        }],
        "solver": {
            "timeStepRatio": 0.25,
            "raysPerPoint": 32,
            "cpuThreads": 1,
            "random": {"mode": "fixed", "seed": 7},
            "enginePreference": "cpu",
            "timeoutSeconds": 30,
        },
        "view": {"panX": 0.0, "panH": 0.0, "zoom": 1.0, "showGrid": True},
    }


def trench_project(name: str) -> dict:
    document = _base(name)
    document["geometry"] = {
        "kind": "trench",
        "width": 1.0,
        "depth": 2.0,
        "taperAngle": 0.0,
        "maskHeight": 0.0,
        "maskTaperAngle": 0.0,
        "substrateMaterial": "Si",
        # maskMaterial omitted: the server default is "Mask".
    }
    return document


def fin_project(name: str) -> dict:
    document = _base(name)
    document["geometry"] = {
        "kind": "fin",
        "width": 1.0,
        "height": 1.2,
        "substrateMaterial": "Si",
        # taperAngle / maskHeight / maskTaperAngle / maskMaterial /
        # construction omitted.
    }
    return document


def stack_project(name: str) -> dict:
    document = _base(name)
    document["dimension"] = 3
    document["domain"] = {
        "gridDelta": 0.2,
        "xExtent": 6.0,
        "yExtent": 4.0,
        "boundaryXY": "reflective",
    }
    document.pop("view")
    document["view3D"] = {}
    document["geometry"] = {
        "kind": "stack",
        "substrateMaterial": "Si",
        # baseHeight omitted: the server default is 0.
        "layers": [
            {"id": "layer_mask", "name": "mask", "material": "Si3N4", "thickness": 0.4, "role": "mask"},
        ],
        "pattern": {"kind": "trench", "width": 1.0, "taperAngle": 0.0},
    }
    return document


class ProjectNormalizeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.lifespan = app.router.lifespan_context(app)
        await self.lifespan.__aenter__()
        self.suffix = uuid.uuid4().hex[:8]

    async def asyncTearDown(self) -> None:
        await self.lifespan.__aexit__(None, None, None)

    async def normalize(self, payload: dict):
        return await asgi_request(NORMALIZE, method="POST", payload=payload)

    async def test_trench_gains_mask_material_and_step_rate_mode(self) -> None:
        response = await self.normalize(trench_project(f"norm trench {self.suffix}"))
        self.assertEqual(response[0], 200, response[2])
        body = json_body(response)
        document = body["project"]
        self.assertEqual(document["geometry"]["maskMaterial"], "Mask")
        self.assertEqual(document["steps"][0]["rateMode"], "uniform")
        # Solver defaults the frontend fingerprint also pads are present.
        self.assertEqual(document["solver"]["advection"]["spatialScheme"], "ENGQUIST_OSHER_1ST_ORDER")
        self.assertEqual(document["solver"]["rayTracing"]["maxBoundaryHits"], 1000)
        self.assertNotIn(b"null", response[2])

    async def test_fin_gains_taper_mask_and_construction_defaults(self) -> None:
        response = await self.normalize(fin_project(f"norm fin {self.suffix}"))
        self.assertEqual(response[0], 200, response[2])
        geometry = json_body(response)["project"]["geometry"]
        self.assertEqual(geometry["taperAngle"], 0)
        self.assertEqual(geometry["maskHeight"], 0)
        self.assertEqual(geometry["maskTaperAngle"], 0)
        self.assertEqual(geometry["maskMaterial"], "Mask")
        self.assertEqual(geometry["construction"], "native")

    async def test_stack_gains_base_height(self) -> None:
        response = await self.normalize(stack_project(f"norm stack {self.suffix}"))
        self.assertEqual(response[0], 200, response[2])
        document = json_body(response)["project"]
        self.assertEqual(document["geometry"]["baseHeight"], 0)
        self.assertEqual(document["dimension"], 3)

    async def test_normalize_is_idempotent_and_matches_a_saved_project(self) -> None:
        name = f"norm saved {self.suffix}"
        first = await self.normalize(trench_project(name))
        self.assertEqual(first[0], 200, first[2])
        normalized = json_body(first)["project"]

        again = await self.normalize(normalized)
        self.assertEqual(again[0], 200, again[2])
        self.assertEqual(json_body(again)["project"], normalized)

        created = await asgi_request(PROJECTS, method="POST", payload=trench_project(name))
        self.assertEqual(created[0], 201, created[2])
        stored = await asgi_request(f"{PROJECTS}/{json_body(created)['projectId']}")
        self.assertEqual(stored[0], 200, stored[2])
        self.assertEqual(json_body(stored), normalized)

    async def test_invalid_documents_are_422(self) -> None:
        cases = (
            {**trench_project("bad"), "schemaVersion": 6},
            {**trench_project("bad"), "bogusField": 1},
            {key: value for key, value in trench_project("bad").items() if key != "steps"},
            {**trench_project("bad"), "steps": [etch_step(duration=0.0)]},
            {**trench_project("bad"), "steps": [etch_step(), etch_step()]},
            {**fin_project("bad"), "geometry": {"kind": "fin", "width": 0.1, "height": 1.2, "substrateMaterial": "Si"}},
        )
        for case in cases:
            with self.subTest(case=sorted(case)):
                response = await self.normalize(case)
                self.assertEqual(response[0], 422, response[2])

    async def test_normalize_writes_nothing_and_compiles_nothing(self) -> None:
        from backend.workflow.api import get_store  # noqa: E402

        tables = ("projects", "compiles", "routes", "attempts", "segments")

        def counts() -> dict[str, int]:
            with sqlite3.connect(get_store().path) as connection:
                return {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in tables
                }

        before_projects = json_body(await asgi_request(PROJECTS))["projects"]
        before_compiles = json_body(await asgi_request("/api/v1/workflow/compiles"))["compiles"]
        before_routes = json_body(await asgi_request("/api/v1/workflow/routes"))["routes"]
        before_counts = counts()
        for document in (
            trench_project(f"norm read-only trench {self.suffix}"),
            fin_project(f"norm read-only fin {self.suffix}"),
            stack_project(f"norm read-only stack {self.suffix}"),
        ):
            response = await self.normalize(document)
            self.assertEqual(response[0], 200, response[2])

        self.assertEqual(json_body(await asgi_request(PROJECTS))["projects"], before_projects)
        self.assertEqual(json_body(await asgi_request("/api/v1/workflow/compiles"))["compiles"], before_compiles)
        self.assertEqual(json_body(await asgi_request("/api/v1/workflow/routes"))["routes"], before_routes)
        self.assertEqual(counts(), before_counts)


if __name__ == "__main__":
    unittest.main()
