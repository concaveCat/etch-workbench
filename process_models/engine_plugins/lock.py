"""Lock file for installed engine plugins.

``engines.lock`` pins the manifest bytes and the engine package implementation
bytes.  Loading refuses a missing/mutated/drifting plugin instead of falling
back to another provider.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ENGINE_LOCK_FILENAME",
    "EngineLock",
    "EngineLockEntry",
    "compute_engine_hash",
    "read_engine_lock",
    "write_engine_lock",
]

ENGINE_LOCK_FILENAME = "engines.lock"
_SCHEMA_VERSION = 1
_LOCK_ENV = "VIENNAPS_WORKBENCH_ENGINE_LOCK"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class EngineLockEntry:
    engine_id: str
    version: str
    entrypoint: str
    manifest_sha256: str
    implementation_hash: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.engine_id, self.version)


@dataclass(frozen=True)
class EngineLock:
    entries: tuple[EngineLockEntry, ...]
    schema_version: int = _SCHEMA_VERSION

    def by_key(self) -> dict[tuple[str, str], EngineLockEntry]:
        return {entry.key: entry for entry in self.entries}

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "engines": [
                {
                    "engineId": entry.engine_id,
                    "version": entry.version,
                    "entrypoint": entry.entrypoint,
                    "manifestSha256": entry.manifest_sha256,
                    "implementationHash": entry.implementation_hash,
                }
                for entry in sorted(self.entries, key=lambda item: item.key)
            ],
        }


def default_lock_path() -> Path:
    raw = os.environ.get(_LOCK_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else (_REPO_ROOT / path)
    return _REPO_ROOT / ENGINE_LOCK_FILENAME


def read_engine_lock(path: str | os.PathLike[str] | None = None) -> EngineLock:
    lock_path = Path(path) if path is not None else default_lock_path()
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read engine lock {lock_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON in engine lock {lock_path}: {error}") from error
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "engines"}:
        raise ValueError(f"engine lock {lock_path} must contain exactly schemaVersion and engines")
    if raw["schemaVersion"] != _SCHEMA_VERSION:
        raise ValueError(f"engine lock {lock_path}: unsupported schemaVersion {raw['schemaVersion']!r}")
    engines = raw["engines"]
    if not isinstance(engines, list):
        raise ValueError(f"engine lock {lock_path}: engines must be a list")
    entries: list[EngineLockEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(engines):
        if not isinstance(item, dict):
            raise ValueError(f"engine lock {lock_path}: engines[{index}] must be an object")
        required = {"engineId", "version", "entrypoint", "manifestSha256", "implementationHash"}
        if set(item) != required:
            raise ValueError(f"engine lock {lock_path}: engines[{index}] keys must be {sorted(required)}")
        entry = EngineLockEntry(
            engine_id=_string(item["engineId"], lock_path, index, "engineId"),
            version=_string(item["version"], lock_path, index, "version"),
            entrypoint=_string(item["entrypoint"], lock_path, index, "entrypoint"),
            manifest_sha256=_string(item["manifestSha256"], lock_path, index, "manifestSha256"),
            implementation_hash=_string(item["implementationHash"], lock_path, index, "implementationHash"),
        )
        if entry.key in seen:
            raise ValueError(f"engine lock {lock_path}: duplicate engine {entry.key!r}")
        seen.add(entry.key)
        entries.append(entry)
    return EngineLock(tuple(entries))


def write_engine_lock(lock: EngineLock, path: str | os.PathLike[str] | None = None) -> Path:
    lock_path = Path(path) if path is not None else default_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = lock_path.with_name(lock_path.name + ".tmp")
    temporary.write_text(json.dumps(lock.payload(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, lock_path)
    return lock_path


def compute_engine_hash(manifest_path: str | os.PathLike[str]) -> tuple[str, str]:
    """Return ``(manifest_sha256, implementation_hash)`` for one engine plugin."""
    manifest = Path(manifest_path)
    manifest_bytes = manifest.read_bytes()
    plugin_dir = manifest.parent
    digest = hashlib.sha256()
    for path in sorted(plugin_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(plugin_dir).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return hashlib.sha256(manifest_bytes).hexdigest(), digest.hexdigest()


def _string(value: object, path: Path, index: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"engine lock {path}: engines[{index}].{field} must be a non-empty string")
    return value
