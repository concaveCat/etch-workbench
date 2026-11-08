#!/usr/bin/env bash
set -euo pipefail
workbench_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$workbench_root/frontend"
npm ci --offline
npm run build
