#!/usr/bin/env bash
# Local style/contract gate for the modularized surface.
# Deliberately narrow: only the new/modernized modules are linted, legacy files
# are excluded in pyproject.toml, and the full Python suite is left to the
# isolated-data-dir task.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python_bin="${PYTHON:-.venv/bin/python}"

# ruff is a dev-only tool (requirements-dev.txt). Missing ruff is a hard failure:
# a gate that silently skips linting reports success it did not verify.
if command -v ruff >/dev/null 2>&1; then
  ruff_cmd=(ruff)
elif "$python_bin" -m ruff --version >/dev/null 2>&1; then
  ruff_cmd=("$python_bin" -m ruff)
else
  echo "ruff not found; install dev tools with: $python_bin -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

targets=(backend/api backend/api_errors.py backend/packages.py backend/planning.py
         backend/supervisor.py engine_plugins worker/artifacts.py worker/engine_policy.py
         worker/steps worker/v2_snapshot.py worker/v2_state.py process_models plugins
         scripts/lock-engines.py scripts/lock-plugins.py)

echo "== compileall =="
"$python_bin" -m compileall -q backend worker scripts process_models plugins

echo "== ruff =="
"${ruff_cmd[@]}" check "${targets[@]}"
"${ruff_cmd[@]}" format --check "${targets[@]}"

echo "== frontend typecheck =="
(cd frontend && npm run typecheck)

echo "== git diff --check =="
git diff --check

echo "style gate passed"
