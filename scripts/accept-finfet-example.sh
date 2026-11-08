#!/usr/bin/env bash
# FinFET.py acceptance: native reference + full workbench project + replay.
#
# Usage:
#   scripts/accept-finfet-example.sh                  # reference, project, replay
#   scripts/accept-finfet-example.sh --skip-reference # reuse committed expected.json
#
# The script keeps every runtime artifact below var/ and never touches the
# production data directory or the production ports.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
STAMP="$(date +%s)"
DATA_DIR="$ROOT/var/accept-finfet-$STAMP"
REFERENCE_DIR="$DATA_DIR/reference"
PORT="${WORKBENCH_PORT:-8767}"
SKIP_REFERENCE=0

for arg in "$@"; do
  case "$arg" in
    --skip-reference) SKIP_REFERENCE=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

mkdir -p "$DATA_DIR"
echo "data directory: $DATA_DIR"
echo "port: $PORT"

if [[ "$SKIP_REFERENCE" -eq 0 ]]; then
  echo "== native reference: original nanometre script =="
  "$PY" docs/finfet-example/reference/run_reference.py \
    --output-dir "$REFERENCE_DIR/nm" \
    --expected "$REFERENCE_DIR/nm/expected.json"
  "$PY" - "$REFERENCE_DIR/nm/expected.json" docs/finfet-example/expected.json <<'PY'
import json, sys
from pathlib import Path
produced = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
committed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if produced["referenceScript"]["sha256"] != committed["referenceScript"]["sha256"]:
    raise SystemExit("reference script hash differs from the committed fixture")
if [row["digest"] for row in produced["surfaces"]] != [row["digest"] for row in committed["surfaces"]]:
    raise SystemExit("reference surface digests differ from the committed fixture")
if produced["volume"]["digest"] != committed["volume"]["digest"]:
    raise SystemExit("reference volume digest differs from the committed fixture")
print("original nanometre reference matches expected.json")
PY

  echo "== native reference: project-frame micrometre twin =="
  "$PY" docs/finfet-example/reference/run_reference.py \
    --script docs/finfet-example/reference/FinFET_um.py \
    --coordinate-unit um \
    --output-dir "$REFERENCE_DIR/um" \
    --expected "$REFERENCE_DIR/um/expected.json"
  "$PY" - "$REFERENCE_DIR/um/expected.json" docs/finfet-example/expected-um.json <<'PY'
import json, sys
from pathlib import Path
produced = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
committed = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if produced["referenceScript"]["sha256"] != committed["referenceScript"]["sha256"]:
    raise SystemExit("project-frame reference script hash differs from the committed fixture")
if [row["digest"] for row in produced["surfaces"]] != [row["digest"] for row in committed["surfaces"]]:
    raise SystemExit("project-frame surface digests differ from the committed fixture")
if produced["volume"]["digest"] != committed["volume"]["digest"]:
    raise SystemExit("project-frame volume digest differs from the committed fixture")
print("project-frame reference matches expected-um.json")
PY
fi

echo "== workbench project + replay =="
set +e
VIENNAPS_WORKBENCH_V2_EXECUTION=1 \
VIENNAPS_WORKBENCH_RUN_FINFET_ACCEPTANCE=1 \
VIENNAPS_WORKBENCH_DATA_DIR="$DATA_DIR" \
WORKBENCH_PORT="$PORT" \
"$PY" -m unittest tests.acceptance.test_finfet_example -v
status=$?
set -e

if [[ "$status" -eq 0 ]]; then
  echo "PASS: FinFET surfaces, volume, v2 records and replay match the native reference"
else
  echo "FAIL: acceptance failed; inspect $DATA_DIR and the unittest log above" >&2
fi
exit "$status"
