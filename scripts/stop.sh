#!/usr/bin/env bash
set -euo pipefail
workbench_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$workbench_root/.venv/bin/python" "$workbench_root/scripts/manage_server.py" stop
