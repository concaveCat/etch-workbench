#!/usr/bin/env bash
set -euo pipefail
workbench_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workbench_root"
if [[ -z "${VIENNAPS_PATCHED_RUNTIME_DIR:-}" && -d "$workbench_root/runtime-patched/current/viennaps" ]]; then
  export VIENNAPS_PATCHED_RUNTIME_DIR="$workbench_root/runtime-patched/current"
fi
if [[ -z "${VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR:-}" && -d "$workbench_root/runtime-patched/versions" ]]; then
  export VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR="$workbench_root/runtime-patched/versions"
fi
./scripts/status.sh
"$workbench_root/.venv/bin/python" -c 'from backend.native_build import configured_runtime; print("patched ViennaPS runtime:", configured_runtime()[1])'
"$workbench_root/.venv/bin/python" scripts/probe-host-gpu.py --host-execution-confirmed
node frontend/tests/browser-3d.mjs
