#!/usr/bin/env bash
# Seed the accepted 22 nm FinFET bare-wafer demo into a running Workbench.
#
#   BASE_URL=http://127.0.0.1:8871 scripts/seed-finfet22-demo.sh
#
# Steps:
#   1. POST docs/finfet22-bare-wafer/finfet22_05.gds to /api/gds-assets
#      (the returned assetId is the GDS SHA-256).
#   2. POST docs/finfet22-bare-wafer/flow_full.json as a ProjectV7 document to
#      /api/v1/workflow/projects, so it shows up in 01's recent projects.
#   3. Print the projectId and the curl commands for the next step.
#
# The script only talks to an already running server; it never starts or stops
# one. Temporary files live inside <repo>/var and are removed on exit.
# Re-running is safe: an identical project returns the stored record (HTTP 200).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8871}"
GDS="$ROOT/docs/finfet22-bare-wafer/finfet22_05.gds"
FLOW="$ROOT/docs/finfet22-bare-wafer/flow_full.json"

die() {
  echo "seed-finfet22-demo: $*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
[[ -f "$GDS" ]] || die "missing GDS: $GDS"
[[ -f "$FLOW" ]] || die "missing flow: $FLOW"

mkdir -p "$ROOT/var"
WORK_DIR="$(mktemp -d "$ROOT/var/seed-finfet22-XXXXXX")"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

# a) Upload the GDS layout. The store is content-addressed, so assetId is the
#    file's SHA-256 and re-uploading the same file is a no-op.
UPLOAD_BODY="$WORK_DIR/gds-upload.json"
upload_status="$(curl -sS --connect-timeout 5 --max-time 120 \
  -o "$UPLOAD_BODY" -w '%{http_code}' \
  -X POST "$BASE_URL/api/gds-assets?file_name=finfet22_05.gds" \
  -H 'Content-Type: application/octet-stream' \
  --data-binary "@$GDS")" || die "GDS upload failed: cannot reach $BASE_URL"

if [[ "$upload_status" != "201" && "$upload_status" != "200" ]]; then
  echo "seed-finfet22-demo: GDS upload failed (HTTP $upload_status)" >&2
  cat "$UPLOAD_BODY" >&2 || true
  echo >&2
  exit 1
fi

ASSET_ID="$(python3 - "$UPLOAD_BODY" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit(f"cannot parse upload response: {exc}")
asset_id = payload.get("assetId")
if not isinstance(asset_id, str) or not asset_id:
    raise SystemExit(f"upload response has no assetId: {payload!r}")
print(asset_id)
PY
)" || die "GDS upload returned an unexpected body"
echo "assetId: $ASSET_ID"

# b) Build the ProjectV7 body from the accepted flow. flow_full.json already
#    points at this GDS by SHA-256; rewrite the id from the upload response so
#    a regenerated GDS cannot silently bind to a stale asset.
PROJECT_BODY="$WORK_DIR/project.json"
python3 - "$FLOW" "$ASSET_ID" "$PROJECT_BODY" <<'PY' || die "cannot build the project body from flow_full.json"
import json, sys
flow_path, asset_id, out_path = sys.argv[1:4]
with open(flow_path, encoding="utf-8") as handle:
    project = json.load(handle)
if project.get("schemaVersion") != 7:
    raise SystemExit(f"{flow_path} is not a schemaVersion 7 project")
if not isinstance(project.get("steps"), list) or not project["steps"]:
    raise SystemExit(f"{flow_path} has no steps")
geometry = project.get("geometry")
if not isinstance(geometry, dict) or geometry.get("kind") != "gds":
    raise SystemExit(f"{flow_path} geometry is not a GDS binding")
documented = geometry.get("assetId")
if documented != asset_id:
    print(
        f"note: flow_full.json assetId {documented} != uploaded {asset_id}; "
        "writing the uploaded id",
        file=sys.stderr,
    )
geometry["assetId"] = asset_id
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(project, handle, ensure_ascii=False)
PY

PROJECT_RESPONSE="$WORK_DIR/project-response.json"
create_status="$(curl -sS --connect-timeout 5 --max-time 120 \
  -o "$PROJECT_RESPONSE" -w '%{http_code}' \
  -X POST "$BASE_URL/api/v1/workflow/projects" \
  -H 'Content-Type: application/json' \
  --data-binary "@$PROJECT_BODY")" || die "project create failed: cannot reach $BASE_URL"

if [[ "$create_status" != "201" && "$create_status" != "200" ]]; then
  echo "seed-finfet22-demo: project create failed (HTTP $create_status)" >&2
  cat "$PROJECT_RESPONSE" >&2 || true
  echo >&2
  exit 1
fi

PROJECT_ID="$(python3 - "$PROJECT_RESPONSE" "$create_status" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit(f"cannot parse project response: {exc}")
project_id = payload.get("projectId")
if not isinstance(project_id, str) or not project_id.startswith("proj_"):
    raise SystemExit(f"project response has no projectId: {payload!r}")
print(project_id)
if sys.argv[2] == "200":
    print("note: an identical project already existed; reused it", file=sys.stderr)
PY
)" || die "project create returned an unexpected body"

echo "projectId: $PROJECT_ID"
echo
echo "Project is now listed by 01 (GET $BASE_URL/api/v1/workflow/projects)."
echo "Next step:"
cat <<NEXT
  # Read the stored project back
  curl -sS "$BASE_URL/api/v1/workflow/projects/$PROJECT_ID"

  # Register the uploaded layout so the 01 layout/clip flow can use it
  curl -sS -X POST "$BASE_URL/api/v1/workflow/layouts" \\
    -H 'Content-Type: application/json' \\
    -d '{"assetId":"$ASSET_ID","fileName":"finfet22_05.gds"}'

  # Compile the project without a clip (creates the flow + binding)
  curl -sS -X POST "$BASE_URL/api/v1/workflow/flows/from-project" \\
    -H 'Content-Type: application/json' \\
    -d "\$(python3 -c 'import json,sys; print(json.dumps({"project": json.load(sys.stdin)}))' < "$FLOW")"
NEXT
