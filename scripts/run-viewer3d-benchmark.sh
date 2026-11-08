#!/usr/bin/env bash
# Build the standalone viewer3d harness, serve it and run the Playwright benchmark.
#
#   scripts/run-viewer3d-benchmark.sh
#
# Env:
#   VIEWER3D_PORT      preferred port (default 8811, auto-increments when busy)
#   VIEWER3D_BENCH_OUT build directory (default $TMPDIR/viewer3d-bench-dist)
#   VIEWER3D_BENCH_JSON report path (default docs/viewer3d-benchmark.json)
#   VIEWER3D_CDP_URL   attach to a running browser over CDP (e.g. Windows Edge
#                      with the RTX GPU: http://127.0.0.1:9222) instead of
#                      launching headless SwiftShader Chromium
#   KEEP_BUILD=1       keep the built harness after the run
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FRONTEND="$ROOT/frontend"
OUT="${VIEWER3D_BENCH_OUT:-${TMPDIR:-/tmp}/viewer3d-bench-dist}"
PORT="${VIEWER3D_PORT:-8811}"
REPORT="${VIEWER3D_BENCH_JSON:-$ROOT/docs/viewer3d-benchmark.json}"
# Ports owned by other services in this repo.
RESERVED="8769 8789 8819 8820"

port_busy() {
  ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN
}

for reserved in $RESERVED; do
  if [ "$PORT" = "$reserved" ]; then
    echo "端口 $PORT 是保留端口，请用 VIEWER3D_PORT 指定其他端口" >&2
    exit 2
  fi
done

chosen=""
for offset in 0 1 2 3 4 5 6 7 8 9; do
  candidate=$((PORT + offset))
  if ! port_busy "$candidate"; then
    chosen=$candidate
    break
  fi
done
if [ -z "$chosen" ]; then
  echo "从 $PORT 起找不到可用端口" >&2
  exit 2
fi
if [ "$chosen" != "$PORT" ]; then
  echo "端口 $PORT 已被占用，改用 $chosen" >&2
fi

mkdir -p "$OUT"
echo "==> 构建 harness（$OUT）"
cd "$FRONTEND"
VIEWER3D_BENCH_OUT="$OUT" npx vite build --config tests/vite.viewer3d.config.ts

echo "==> 启动静态服务 127.0.0.1:$chosen"
VIEWER3D_BENCH_OUT="$OUT" npx vite preview --config tests/vite.viewer3d.config.ts \
  --host 127.0.0.1 --port "$chosen" --strictPort >"$OUT/preview.log" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  if [ "${KEEP_BUILD:-0}" != "1" ]; then
    rm -rf "$OUT"
  fi
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$chosen/viewer3d-harness.html"; then
    break
  fi
  sleep 0.5
done

echo "==> 运行 benchmark"
if [ -n "${VIEWER3D_CDP_URL:-}" ]; then
  echo "==> CDP 浏览器：$VIEWER3D_CDP_URL（不启动本地 headless）"
else
  echo "==> 本地 headless Chromium + SwiftShader（软件回退）"
fi
status=0
VIEWER3D_BASE_URL="http://127.0.0.1:$chosen" \
  VIEWER3D_CDP_URL="${VIEWER3D_CDP_URL:-}" \
  VIEWER3D_BENCH_JSON="$REPORT" \
  node tests/viewer3d-benchmark.mjs || status=$?
if [ -f "$REPORT" ]; then
  node -e '
    const report = require(process.argv[1]);
    console.log(`==> renderer: ${report.webglRenderer ?? "unknown"}`);
    console.log(`==> environment: ${report.environment ?? "unknown"}`);
  ' "$REPORT" || true
fi
exit "$status"
