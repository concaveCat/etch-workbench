#!/usr/bin/env bash
set -euo pipefail
workbench_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workbench_root"
# Real meshes are prepared before starting a second API instance.
if ! .venv/bin/python scripts/manage_server.py status >/dev/null 2>&1; then
  .venv/bin/python scripts/prepare_browser.py
  ./scripts/start.sh
fi
node frontend/tests/browser-3d.mjs
