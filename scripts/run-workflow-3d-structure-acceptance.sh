#!/usr/bin/env bash
# Isolated Chromium acceptance for the 3D structure timeline in 03.
#
#   scripts/run-workflow-3d-structure-acceptance.sh
#
# Env:
#   WORKFLOW_PORT          preferred port (default 8861)
#   WORKFLOW_SCREENSHOT    full-page screenshot (default $ROOT/var/workflow-3d-structure.png)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${WORKFLOW_PORT:-8861}"
DATA="${WORKFLOW_DATA_DIR:-$ROOT/var/test-workflow-3d-structure-$(date +%s)}"
SCREENSHOT="${WORKFLOW_SCREENSHOT:-$ROOT/var/workflow-3d-structure.png}"
cleanup() {
  WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" "$ROOT/scripts/stop.sh" >/dev/null 2>&1 || true
  rm -rf "$DATA"
}
trap cleanup EXIT
mkdir -p "$ROOT/var"
cd "$ROOT"
WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" ./scripts/start.sh >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then break; fi
  sleep 0.5
done
cd frontend
WORKBENCH_URL="http://127.0.0.1:$PORT" WORKFLOW_SCREENSHOT="$SCREENSHOT" \
  node tests/workflow-3d-structure-live.mjs
