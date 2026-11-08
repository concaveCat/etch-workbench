"""Identify the isolated patched ViennaPS runtime without importing it."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from .engine import PATCHED_RUNTIME_VERSIONS, version_label


def fingerprint_package(package: Path) -> str:
    package = package.resolve(strict=True)
    extensions = sorted(package.glob("_core*.so"))
    kernels = sorted((package / "ptx").glob("*.optixir")) + sorted((package / "ptx").glob("*.ptx"))
    python_files = sorted(package.rglob("*.py"))
    gpu_abi = package / "libViennaLS_GPU.so"
    dependencies = package.parent / "viennaps.libs"
    libraries = sorted(path for path in dependencies.glob("*.so*") if path.is_file())
    if (len(extensions) != 1 or not kernels or not (package / "__init__.py").is_file() or not gpu_abi.is_file()
            or not (dependencies / "libembree4.so").is_file()
            or not any(path.name.startswith("libtbb") for path in libraries)):
        raise RuntimeError("patched ViennaPS runtime is missing its extension, kernels, ViennaLS GPU or bundled Embree/TBB libraries")
    files = [extensions[0], gpu_abi, *kernels, *python_files]
    hashes = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    hashes.update({f"viennaps.libs/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest() for path in libraries})
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_roots() -> list[Path]:
    value = os.environ.get("VIENNAPS_PATCHED_RUNTIME_DIR")
    if not value:
        raise RuntimeError("patched ViennaPS multi-ion GPU runtime is not configured")
    roots = [Path(value).expanduser().resolve(strict=True)]
    history_value = os.environ.get("VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR")
    if history_value:
        history = Path(history_value).expanduser().resolve(strict=True)
        roots.extend(sorted((path.resolve() for path in history.iterdir() if path.is_dir()), key=str))
    return list(dict.fromkeys(roots))


def configured_runtime(expected_fingerprint: str | None = None) -> tuple[Path, str]:
    """Resolve the current engine or an immutable runtime required by replay.

    Deployments may retain prior engine package roots in
    ``VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR``. A replay pins its original
    fingerprint and must load that exact package rather than failing solely
    because a newer runtime is now current.
    """
    failures = []
    for root in _runtime_roots():
        packages = [d for d in importlib.metadata.distributions(path=[str(root)])
                    if d.metadata.get("Name", "").lower() == "viennaps"]
        if len(packages) != 1 or packages[0].version not in PATCHED_RUNTIME_VERSIONS:
            failures.append(
                f"{root} is not a supported patched ViennaPS runtime "
                f"({version_label(PATCHED_RUNTIME_VERSIONS)}); found "
                f"{packages[0].version if len(packages) == 1 else 'no unique viennaps distribution'}")
            continue
        try:
            fingerprint = fingerprint_package(root / "viennaps")
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append(f"{root}: {exc}")
            continue
        if expected_fingerprint is None or fingerprint == expected_fingerprint:
            return root, fingerprint
    if expected_fingerprint is not None:
        raise RuntimeError(f"no retained ViennaPS runtime matches replay fingerprint {expected_fingerprint}")
    detail = "; ".join(failures)
    raise RuntimeError(detail or "no valid patched ViennaPS multi-ion runtime is configured")
