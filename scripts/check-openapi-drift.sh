#!/usr/bin/env bash
# Re-export the OpenAPI schema and verify it matches the committed copy.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT

"${PROJECT_ROOT}/backend/.venv/bin/python" "${PROJECT_ROOT}/scripts/export-openapi.py" "${TMP_FILE}"
diff -u "${PROJECT_ROOT}/docs/openapi.json" "${TMP_FILE}"
