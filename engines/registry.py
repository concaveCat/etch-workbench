"""Engine registry: how to launch each engine and what it claims to support.

Both engines are subprocesses on purpose. ViennaPS runs in the workbench
virtualenv; SPX links its own libpython 3.11 and its own shared objects, so it
must run under ``$SPX/linux64/bin/python3``. The scheduler never imports either
engine in-process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SPX_HOME = Path(os.environ.get("SPX_HOME", "/root/SPX/spx/V-2024.03/linux64"))
LICENSE_FILE = os.environ.get("SNPSLMD_LICENSE_FILE", "27000@localhost")


@dataclass(frozen=True)
class EngineRegistration:
    engine_id: str
    label: str
    command: tuple[str, ...]
    adapter: str
    mechanisms: tuple[str, ...]
    frame_kinds: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    rationale: str = ""

    def launch(self, job_path: Path, out_dir: Path) -> list[str]:
        return [*self.command, str(job_path), str(out_dir)]


def spx_env() -> dict[str, str]:
    lib = str(SPX_HOME / "lib")
    binaries = str(SPX_HOME / "bin")
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    return {
        "SNPSLMD_LICENSE_FILE": LICENSE_FILE,
        "LD_LIBRARY_PATH": ":".join(p for p in (lib, binaries, existing) if p),
        # WSL2 mirrored networking drops SYNs to an unbound port, so any blocking
        # connect() hangs forever; the preload turns that into ECONNREFUSED.
        "LD_PRELOAD": os.environ.get("LD_PRELOAD", "/opt/eda-patches/libfastrefuse.so"),
    }


def registrations() -> dict[str, EngineRegistration]:
    spx_python = str(SPX_HOME / "bin" / "python3")
    return {
        "viennaps": EngineRegistration(
            engine_id="viennaps", label="ViennaPS (reference)",
            command=(str(REPO_ROOT / ".venv" / "bin" / "python"),
                     str(REPO_ROOT / "engines" / "viennaps" / "engine.py")),
            adapter="engines.viennaps.engine",
            mechanisms=("initial", "isotropic"),
            frame_kinds=("mesh",),
            rationale="level-set reference; full-grid solve, seconds per step",
        ),
        "spx": EngineRegistration(
            engine_id="spx", label="Sentaurus Process Explorer (fast)",
            command=(spx_python, str(REPO_ROOT / "engines" / "spx" / "bridge.py")),
            adapter="engines.spx.bridge",
            mechanisms=("initial", "isotropic", "anisotropic", "cmp"),
            frame_kinds=("raster",),
            env=spx_env(),
            rationale="geometric/empirical emulation; one session licence (spx_all) per run",
        ),
    }


def describe() -> list[dict[str, Any]]:
    return [
        {"engineId": reg.engine_id, "label": reg.label, "adapter": reg.adapter,
         "mechanisms": list(reg.mechanisms), "frameKinds": list(reg.frame_kinds),
         "rationale": reg.rationale}
        for reg in registrations().values()
    ]
