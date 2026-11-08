"""Heavy FinFET.py acceptance run (opt-in).

Set ``VIENNAPS_WORKBENCH_RUN_FINFET_ACCEPTANCE=1`` to execute the 25-step
nanometre schema-7 fixture through the API, compare all 26 surface checkpoints
and the final volume mesh with the original native nm reference, then replay
the run and compare the final states again.

The default fast suite never imports or runs this module's test class because
the native reference is committed as ``expected.json`` and the full workbench
run takes tens of minutes and several gigabytes of volume artifacts.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

import viennaps as ps

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docs" / "finfet-example" / "reference"))

from canonical import surface_digest, volume_digest  # noqa: E402

from backend import snapshots as snapshot_store  # noqa: E402
from backend import storage  # noqa: E402
from backend.main import app  # noqa: E402
from tests.test_api_integration import asgi_request, json_body  # noqa: E402

ACCEPT_ENV = "VIENNAPS_WORKBENCH_RUN_FINFET_ACCEPTANCE"
PROJECT = ROOT / "docs" / "finfet-example" / "project.json"
EXPECTED_NM = ROOT / "docs" / "finfet-example" / "expected.json"
EXPECTED_UM = ROOT / "docs" / "finfet-example" / "expected-um.json"
REFERENCE_FRAME_ENV = "VIENNAPS_WORKBENCH_FINFET_REFERENCE_FRAME"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


def reference_path() -> Path:
    frame = os.environ.get(REFERENCE_FRAME_ENV, "nm").strip().lower()
    if frame == "nm":
        return EXPECTED_NM
    if frame == "um":
        # The micrometre twin is retained to document the native scale
        # sensitivity; the project itself is authored in nanometres.
        return EXPECTED_UM
    raise RuntimeError(f"{REFERENCE_FRAME_ENV} must be 'nm' or 'um', got {frame!r}")


def load_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        json.loads(PROJECT.read_text(encoding="utf-8")),
        json.loads(reference_path().read_text(encoding="utf-8")),
    )


def read_volume(path: Path) -> dict[str, Any]:
    mesh = ps.ls.Mesh()
    reader = ps.ls.VTKReader()
    reader.setFileName(str(path))
    reader.setMesh(mesh)
    reader.apply()
    nodes = mesh.getNodes()
    tetras = mesh.getTetras()
    hexas = mesh.getHexas()
    if tetras and hexas:
        raise AssertionError(f"volume {path} mixes tetrahedra and hexahedra")
    cells = tetras or hexas
    if not cells:
        raise AssertionError(f"volume {path} has no 3D cells")
    data = mesh.getCellData()
    material_ids: list[int] = []
    for index in range(data.getScalarDataSize()):
        if str(data.getScalarDataLabel(index)) == "Material":
            material_ids = [int(value) for value in data.getScalarData(index)]
    if len(material_ids) != len(cells):
        raise AssertionError("volume material data is not aligned with its cells")
    counts: dict[str, int] = {}
    for value in material_ids:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return {
        "nodeCount": len(nodes),
        "cellCount": len(cells),
        "cellType": "tetra" if tetras else "hexa",
        "materialCounts": dict(sorted(counts.items(), key=lambda item: int(item[0]))),
        "digest": volume_digest(nodes, cells, material_ids),
    }


def volume_output(run_dir: Path, snapshot: dict[str, Any]) -> Path:
    rows = [row for row in snapshot.get("outputs", []) if row.get("kind") == "volumeMesh"]
    if not rows:
        raise AssertionError("final snapshot has no volumeMesh output")
    return run_dir / str(rows[-1]["fileName"])


@unittest.skipUnless(
    os.environ.get(ACCEPT_ENV) == "1",
    f"set {ACCEPT_ENV}=1 to run the full FinFET acceptance fixture",
)
class FinFETAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        storage.initialize()
        self.project, self.expected = load_fixture()
        self.life = app.router.lifespan_context(app)
        await self.life.__aenter__()

    async def asyncTearDown(self) -> None:
        await self.life.__aexit__(None, None, None)

    async def wait_terminal(self, run_id: str) -> dict[str, Any]:
        # 7200 * 0.5 s = one hour; solver.timeoutSeconds is 3600.
        for _ in range(7200):
            info = json_body(await asgi_request(f"/api/runs/{run_id}"))
            if info["status"] in TERMINAL:
                return info
            await asyncio.sleep(0.5)
        self.fail(f"FinFET run {run_id} did not reach a terminal state")

    async def run_project(self, project: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        response = await asgi_request("/api/runs", method="POST",
                                      payload={"kind": "simulate", "project": project})
        self.assertEqual(response[0], 202, response[2])
        run_id = json_body(response)["runId"]
        info = await self.wait_terminal(run_id)
        self.assertEqual(info["status"], "succeeded", info.get("failureReason"))
        return run_id, info

    async def assert_surfaces(self, run_id: str) -> None:
        run_dir = storage.run_path(run_id)
        files = ["initial"] + [step["id"] for step in self.project["steps"]]
        self.assertEqual(len(files), 26)
        for index, step_id in enumerate(files):
            snapshot_store.ensure_materialized(run_dir, step_id)
            snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{step_id}"))
            expected = self.expected["surfaces"][index]
            context = f"surface {index} ({step_id})"
            self.assertEqual(len(snapshot["points"]), expected["pointCount"], context)
            self.assertEqual(len(snapshot["triangles"]), expected["triangleCount"], context)
            self.assertEqual(snapshot["materialIdsByLevelSetIndex"], expected["materialMap"], context)
            digest = surface_digest(snapshot["points"], snapshot["triangles"],
                                    snapshot["cellData"]["materialIds"])
            self.assertEqual(digest, expected["digest"], context)

    async def assert_final_volume(self, run_id: str) -> str:
        run_dir = storage.run_path(run_id)
        last = self.project["steps"][-1]["id"]
        snapshot_store.ensure_materialized(run_dir, last)
        snapshot = json_body(await asgi_request(f"/api/runs/{run_id}/snapshots/{last}"))
        actual = read_volume(volume_output(run_dir, snapshot))
        expected = self.expected["volume"]
        self.assertEqual(actual["nodeCount"], expected["nodeCount"])
        self.assertEqual(actual["cellCount"], expected["cellCount"])
        self.assertEqual(actual["cellType"], expected["cellType"])
        self.assertEqual(actual["materialCounts"], expected["materialCounts"])
        self.assertEqual(actual["digest"], expected["digest"])
        return actual["digest"]

    def assert_v2_records(self, info: dict[str, Any]) -> None:
        records = info.get("v2Steps") or []
        by_id = {row["stepId"]: row for row in records}
        expected_ids = {step["id"] for step in self.project["steps"]}
        self.assertEqual(set(by_id), expected_ids)
        for step_id in expected_ids:
            record = by_id[step_id]
            self.assertIsNotNone(record.get("plan"), step_id)
            self.assertIsNotNone(record.get("outputs"), step_id)
            self.assertIsNotNone(record.get("state"), step_id)

    async def test_finfet_surfaces_volume_v2_records_and_replay(self) -> None:
        run_id, info = await self.run_project(self.project)
        print(f"FinFET acceptance run id: {run_id}")
        self.assert_v2_records(info)
        await self.assert_surfaces(run_id)
        first_volume = await self.assert_final_volume(run_id)
        print(f"FinFET final volume digest: {first_volume}")

        replay_request = json_body(await asgi_request(f"/api/runs/{run_id}/replay-project"))
        response = await asgi_request("/api/runs/replay", method="POST", payload=replay_request)
        self.assertEqual(response[0], 202, response[2])
        replay_id = json_body(response)["runId"]
        print(f"FinFET replay run id: {replay_id}")
        replay_info = await self.wait_terminal(replay_id)
        self.assertEqual(replay_info["status"], "succeeded", replay_info.get("failureReason"))
        self.assert_v2_records(replay_info)
        await self.assert_surfaces(replay_id)
        self.assertEqual(await self.assert_final_volume(replay_id), first_volume)


if __name__ == "__main__":
    unittest.main()
