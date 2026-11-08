#!/usr/bin/env bash
set -euo pipefail
workbench_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_root="$workbench_root/runtime-patched"
if [[ -z "${VIENNAPS_PATCHED_RUNTIME_DIR:-}" && -d "$runtime_root/current/viennaps" ]]; then
  export VIENNAPS_PATCHED_RUNTIME_DIR="$runtime_root/current"
fi
if [[ -z "${VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR:-}" && -d "$runtime_root/versions" ]]; then
  export VIENNAPS_PATCHED_RUNTIME_HISTORY_DIR="$runtime_root/versions"
fi
exec "$workbench_root/.venv/bin/python" "$workbench_root/scripts/manage_server.py" start
