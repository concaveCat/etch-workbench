#!/usr/bin/env bash
# Isolated Chromium acceptance for the IA batch 3 review items (04/05/06).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${WORKFLOW_PORT:-8812}"
DATA="${WORKFLOW_DATA_DIR:-$ROOT/var/test-workflow-ia-batch3-$(date +%s)}"
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
WORKBENCH_URL="http://127.0.0.1:$PORT" WORKFLOW_SCREENSHOT="${WORKFLOW_SCREENSHOT:-/tmp/workflow-ia-batch3.png}" node tests/workflow-ia-batch3-live.mjs
