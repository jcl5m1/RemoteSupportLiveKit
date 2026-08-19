#!/usr/bin/env bash
# Run all project test suites and lint checks locally.
# Assumes Python 3.12, backend/.venv, agent/.venv, and Flutter are available.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== backend tests + lint ==="
(
  cd "${PROJECT_ROOT}/backend"
  .venv/bin/pytest -q
  .venv/bin/ruff check .
  .venv/bin/mypy app
)

echo "=== agent tests + lint ==="
(
  cd "${PROJECT_ROOT}/agent"
  .venv/bin/pytest -q
  .venv/bin/ruff check .
)

echo "=== Flutter analyze + tests ==="
(
  cd "${PROJECT_ROOT}/mobile"
  if [ -x "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" ]; then
    "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" analyze --no-fatal-infos
    "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" test
  else
    echo "Flutter SDK not found at ${PROJECT_ROOT}/.flutter-sdk/flutter; skipping Flutter checks."
  fi
)

echo "=== all checks passed ==="
