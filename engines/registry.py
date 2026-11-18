"""Pluggable engine registry: declarative discovery + programmatic registration.

Engines run as isolated subprocesses so conflicting native runtimes (SPX-private
Python 3.11 + libpython, ViennaPS C++/CUDA bindings, custom PyTorch/Warp/C++ solvers)
never share an address space.

Discovery sources (in priority order, later overrides earlier on matching id):

1. Built-in ``engines/*/manifest.json`` packages inside this repository;
2. External engine directories listed in ``VIENNAPS_ENGINE_DIRS`` (``os.pathsep``
   or comma separated, each containing ``manifest.json`` or ``*/manifest.json``);
3. Runtime registrations added via :func:`register_engine` (for tests or embedded
   experiments).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINES_DIR = Path(__file__).resolve().parent
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
    role: str = "candidate"
    solve_mode: str = "stepwise"
    probe_method: str = "unknown"
    env: dict[str, str] = field(default_factory=dict)
    rationale: str = ""
    limits: dict[str, float] = field(default_factory=dict)
    manifest_path: str | None = None

    def launch(self, job_path: Path, out_dir: Path) -> list[str]:
        return [*self.command, str(job_path), str(out_dir)]


_RUNTIME_REGISTRATIONS: dict[str, EngineRegistration] = {}


def register_engine(reg: EngineRegistration) -> None:
    """Register or override an engine at runtime."""
    _RUNTIME_REGISTRATIONS[reg.engine_id] = reg


def unregister_engine(engine_id: str) -> None:
    """Remove a runtime-registered engine override."""
    _RUNTIME_REGISTRATIONS.pop(engine_id, None)


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


def _resolve_command(manifest: dict[str, Any], base_dir: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    runtime = str(manifest.get("runtime", "venv_python"))
    raw_entry = manifest.get("entrypoint")
    extra_env = {str(k): str(v) for k, v in (manifest.get("env") or {}).items()}

    if "command" in manifest and isinstance(manifest["command"], list):
        cmd = tuple(str(part) for part in manifest["command"])
        return cmd, extra_env

    if not raw_entry:
        raise ValueError(f"engine manifest in {base_dir} needs 'entrypoint' or 'command'")
    entry_path = Path(raw_entry)
    if not entry_path.is_absolute():
        candidate = (base_dir / entry_path).resolve()
        entry_path = candidate if candidate.exists() else (REPO_ROOT / entry_path).resolve()

    if runtime == "venv_python":
        venv_py = REPO_ROOT / ".venv" / "bin" / "python"
        py = str(venv_py) if venv_py.exists() else sys.executable
        return (py, str(entry_path)), extra_env
    if runtime == "spx_python":
        spx_py = str(SPX_HOME / "bin" / "python3")
        return (spx_py, str(entry_path)), {**spx_env(), **extra_env}
    if runtime == "system_python":
        return (sys.executable, str(entry_path)), extra_env
    if runtime == "binary":
        return (str(entry_path),), extra_env
    raise ValueError(f"unsupported engine runtime {runtime!r} in {base_dir}")


def load_manifest(manifest_file: Path) -> EngineRegistration:
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    engine_id = str(raw["engine_id"]).strip()
    if not engine_id:
        raise ValueError(f"manifest {manifest_file} has empty engine_id")
    cmd, env = _resolve_command(raw, manifest_file.parent)
    limits = {str(k): float(v) for k, v in (raw.get("limits") or {}).items()}
    return EngineRegistration(
        engine_id=engine_id,
        label=str(raw.get("label") or engine_id),
        command=cmd,
        adapter=str(raw.get("adapter") or engine_id),
        mechanisms=tuple(str(m) for m in (raw.get("mechanisms") or ())),
        frame_kinds=tuple(str(k) for k in (raw.get("frame_kinds") or ("mesh",))),
        role=str(raw.get("role") or "candidate"),
        solve_mode=str(raw.get("solve_mode") or "stepwise"),
        probe_method=str(raw.get("probe_method") or "unknown"),
        env=env,
        rationale=str(raw.get("rationale") or ""),
        limits=limits,
        manifest_path=str(manifest_file),
    )


def _iter_external_manifests() -> list[Path]:
    raw = os.environ.get("VIENNAPS_ENGINE_DIRS", "").strip()
    if not raw:
        return []
    sep = "," if "," in raw and os.pathsep not in raw else os.pathsep
    found: list[Path] = []
    for token in (part.strip() for part in raw.split(sep) if part.strip()):
        root = Path(token)
        if (root / "manifest.json").is_file():
            found.append(root / "manifest.json")
        elif root.is_dir():
            found.extend(sorted(root.glob("*/manifest.json")))
    return found


def registrations() -> dict[str, EngineRegistration]:
    discovered: dict[str, EngineRegistration] = {}
    for manifest_file in sorted(ENGINES_DIR.glob("*/manifest.json")):
        reg = load_manifest(manifest_file)
        discovered[reg.engine_id] = reg
    for manifest_file in _iter_external_manifests():
        reg = load_manifest(manifest_file)
        discovered[reg.engine_id] = reg
    discovered.update(_RUNTIME_REGISTRATIONS)
    # Preserve stable display order: reference/baseline engines first when present.
    priority = {"viennaps": 0, "voxel": 1, "spx": 2}
    ordered_keys = sorted(discovered, key=lambda eid: (priority.get(eid, 10), eid))
    return {eid: discovered[eid] for eid in ordered_keys}


def describe() -> list[dict[str, Any]]:
    return [
        {
            "engineId": reg.engine_id,
            "label": reg.label,
            "role": reg.role,
            "adapter": reg.adapter,
            "solveMode": reg.solve_mode,
            "probeMethod": reg.probe_method,
            "mechanisms": list(reg.mechanisms),
            "frameKinds": list(reg.frame_kinds),
            "limits": dict(reg.limits),
            "rationale": reg.rationale,
        }
        for reg in registrations().values()
    ]
