#!/usr/bin/env python3
"""Generate or verify ``engines.lock`` for installed engine plugins."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from process_models.engine_plugins.lock import (  # noqa: E402
    EngineLock,
    EngineLockEntry,
    compute_engine_hash,
    read_engine_lock,
    write_engine_lock,
)
from process_models.engine_plugins.manifest import default_engine_dirs, read_engine_manifest  # noqa: E402


def _entries():
    entries = []
    seen = set()
    for base in default_engine_dirs():
        if not base.is_dir():
            continue
        for manifest_path in sorted(base.glob("*/engine.toml")):
            if not manifest_path.is_file():
                continue
            manifest = read_engine_manifest(manifest_path)
            if manifest.key in seen:
                raise SystemExit(f"duplicate engine plugin {manifest.key!r} at {manifest_path}")
            seen.add(manifest.key)
            manifest_hash, implementation_hash = compute_engine_hash(manifest_path)
            entries.append(
                EngineLockEntry(
                    engine_id=manifest.engine_id,
                    version=manifest.version,
                    entrypoint=manifest.entrypoint,
                    manifest_sha256=manifest_hash,
                    implementation_hash=implementation_hash,
                )
            )
    return tuple(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args()
    lock = EngineLock(_entries())
    if args.check:
        try:
            existing = read_engine_lock()
        except Exception as error:  # noqa: BLE001
            print(f"engine lock is not readable: {error}", file=sys.stderr)
            return 1
        if existing.payload() != lock.payload():
            print("engines.lock is out of date; run scripts/lock-engines.py", file=sys.stderr)
            return 1
        print("engines.lock is up to date")
        return 0
    path = write_engine_lock(lock)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
