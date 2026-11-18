"""CLI entry point and EngineAdapterBase implementation for the ``voxel`` engine."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from engines.protocol import EngineInfo, Frame, JobSpec, SectionSpec  # noqa: E402
from engines.sdk import EngineAdapterBase, run_adapter_cli  # noqa: E402
from engines.voxel.models import build_default_registry  # noqa: E402
from engines.voxel.state import VoxelState  # noqa: E402

ENGINE_VERSION = "0.2.0"


class VoxelEngineAdapter(EngineAdapterBase[VoxelState]):
    def __init__(self) -> None:
        super().__init__(build_default_registry())

    def info(self) -> EngineInfo:
        return EngineInfo(
            engine_id="voxel",
            version=ENGINE_VERSION,
            dimensions=(3,),
            mechanisms=self.models.supported_mechanisms(),
            frame_kinds=("mesh",),
            adapter="engines.voxel.engine",
            solve_mode="stepwise",
            probe_method="voxel_index",
            limits={
                "numpy": np.__version__,
                "python": sys.version.split()[0],
                "max_active_points": 25_000_000.0,
                "max_ratio": 2000.0,
                "max_area_um2": 25.0,
            },
        )

    def engine_limits(self) -> dict[str, float]:
        return {"max_active_points": 25_000_000.0, "max_ratio": 2000.0, "max_area_um2": 25.0}

    def open_state(self, job: JobSpec, out_dir: Path) -> VoxelState:
        del out_dir
        if job.restore:
            return VoxelState.load_checkpoint(Path(job.restore))
        return VoxelState.create(job)

    def extract_frame(
        self, state: VoxelState, job: JobSpec, out_dir: Path, step_id: str
    ) -> Frame:
        return state.write_mesh_frame(out_dir, step_id, dimensions=job.domain.dimensions)

    def probe_points(self, state: VoxelState, points: list[list[float]]) -> list[str]:
        return state.probe(points)

    def section_heights(
        self, state: VoxelState, spec: SectionSpec
    ) -> dict[str, list[float | None]]:
        return state.section(spec)

    def save_checkpoint(self, state: VoxelState, out_dir: Path, step_id: str) -> str | None:
        return state.save_checkpoint(out_dir, step_id)


def main(argv: list[str] | None = None) -> int:
    return run_adapter_cli(VoxelEngineAdapter(), argv)


if __name__ == "__main__":
    raise SystemExit(main())
