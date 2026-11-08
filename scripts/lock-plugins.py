#!/usr/bin/env python3
"""Generate or verify ``plugins.lock``.

Usage::

    python scripts/lock-plugins.py            # write the lock, print its path
    python scripts/lock-plugins.py --check    # verify, exit 0/1

Plugin directories and the lock path honour ``VIENNAPS_WORKBENCH_PLUGIN_DIRS``
and ``VIENNAPS_WORKBENCH_PLUGIN_LOCK``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from process_models.plugins import PluginLifecycleError, verify_lock, write_lock  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the lock instead of writing it")
    args = parser.parse_args(argv)

    try:
        if args.check:
            verify_lock()
            print("plugin lock verified")
        else:
            print(write_lock())
    except PluginLifecycleError as error:
        print(f"plugin lifecycle error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
