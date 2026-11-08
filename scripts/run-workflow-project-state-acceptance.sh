#!/usr/bin/env bash
# Isolated Chromium acceptance for per-project state restore + global save.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${WORKFLOW_PORT:-8889}"
DATA="${WORKFLOW_DATA_DIR:-$ROOT/var/test-workflow-project-state-$(date +%s)}"
cleanup() {
  WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" "$ROOT/scripts/stop.sh" >/dev/null 2>&1 || true
  rm -rf "$DATA"
}
trap cleanup EXIT
cd "$ROOT"
WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" ./scripts/start.sh >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null; then break; fi
  sleep 0.5
done
cd frontend
WORKBENCH_URL="http://127.0.0.1:$PORT" \
  WORKFLOW_LAYOUT="${WORKFLOW_LAYOUT:-$ROOT/docs/stage5-evidence/two-structure-two-layer.gds}" \
  WORKFLOW_SCREENSHOT="${WORKFLOW_SCREENSHOT:-$ROOT/var/workflow-project-state.png}" \
  node tests/workflow-project-state-live.mjs
