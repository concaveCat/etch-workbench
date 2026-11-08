"""Engine-neutral surface projection for custom engines."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from process_models.v2.state import MaterialState, ProcessState
from process_models.v2.surface import SurfaceData
from worker.v2_snapshot import save_surface_snapshot


class FakeSurfaceBackend:
    id = "fake-surface"

    def extract(self, state: ProcessState) -> SurfaceData:
        del state
        return SurfaceData(
            dimension=2,
            points=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            cells=((0, 1), (1, 2)),
            cell_type="lines",
            material_ids=(10, 10),
            grid_delta=0.1,
            materials={10: "Si"},
        )


class SurfaceSnapshotTest(unittest.TestCase):
    def test_custom_engine_snapshot_has_viewer_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            ctx = SimpleNamespace(
                run_id="run-1",
                run_dir=run_dir,
                project={
                    "schemaVersion": 7,
                    "units": {"length": "um"},
                    "dimension": 2,
                },
            )
            state = ProcessState(materials=MaterialState(order=(10,), names={10: "Si"}))
            metadata = save_surface_snapshot(
                ctx,
                state,
                FakeSurfaceBackend(),
                step={"id": "step_01", "name": "custom"},
                step_index=1,
                extra={"v2Plan": {"engineId": "fake"}},
            )
            payload = json.loads((run_dir / "step_01.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["stepId"], "step_01")
            self.assertEqual(payload["dimension"], 2)
            self.assertEqual(len(payload["points"]), 3)
            self.assertEqual(len(payload["lines"]), 2)
            self.assertEqual(payload["cellData"]["materialIds"], [10, 10])
            self.assertEqual(payload["materials"]["10"]["name"], "Si")
            self.assertEqual(payload["v2Plan"]["engineId"], "fake")
            self.assertEqual(payload["surfaceBackend"], "fake-surface")


if __name__ == "__main__":
    unittest.main()
