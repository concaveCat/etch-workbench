#!/usr/bin/env bash
# Real start -> stop mid-queue -> restart -> resume acceptance for P2 experiments.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${WORKFLOW_PORT:-8819}"
DATA="${WORKFLOW_DATA_DIR:-$ROOT/var/test-workflow-p2-recovery-$(date +%s)}"
BASE="http://127.0.0.1:$PORT"

start_server() {
  WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" "$ROOT/scripts/start.sh" >/dev/null
  for _ in $(seq 1 60); do
    if curl -fsS "$BASE/api/health" >/dev/null; then return 0; fi
    sleep 0.5
  done
  echo "server did not become healthy" >&2
  return 1
}

stop_server() {
  WORKBENCH_PORT="$PORT" VIENNAPS_WORKBENCH_DATA_DIR="$DATA" "$ROOT/scripts/stop.sh" >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do
    if ! curl -fsS "$BASE/api/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.25
  done
}

cleanup() {
  stop_server
  rm -rf "$DATA"
}
trap cleanup EXIT

cd "$ROOT"
start_server
EXPERIMENT="$(python3 scripts/workflow_p2_recovery_client.py create "$BASE" frontend/tests/fixtures/workflow-2d-five.json)"
echo "created $EXPERIMENT; stopping server mid-queue" >&2
stop_server
start_server
python3 scripts/workflow_p2_recovery_client.py verify "$BASE" "$EXPERIMENT"
