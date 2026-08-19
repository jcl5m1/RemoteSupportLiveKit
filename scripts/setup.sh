#!/usr/bin/env bash
# Bootstrap a fresh clone for local development.
# This checks prerequisites, creates Python venvs, installs deps, and runs
# a first-pass test suite. It does NOT install Docker, Xcode, or cloud credentials.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

red() { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }

fail() {
  red "$*"
  exit 1
}

require() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required: $2"
}

echo "Checking prerequisites..."
require python3 "https://docs.python.org/3.12/"
require psql "brew install postgresql@16"

echo "Checking Python 3.12..."
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
if [[ ! "$PYTHON_VERSION" =~ ^3\.12 ]]; then
  fail "Python 3.12 is required; found $PYTHON_VERSION"
fi

echo "Checking Postgres..."
if ! pg_isready -q 2>/dev/null; then
  yellow "Postgres is not running. Start it with: brew services start postgresql@16"
  fail "Postgres must be running to continue"
fi

for db in remote_support remote_support_test; do
  if ! psql -U rs -d "$db" -c "SELECT 1" >/dev/null 2>&1; then
    yellow "Database $db not found for user rs. Create it with:"
    yellow "  createuser -s rs || createuser rs"
    yellow "  createdb -O rs $db"
    fail "Database $db is missing"
  fi
done

echo "Setting up backend virtualenv..."
(
  cd "${PROJECT_ROOT}/backend"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e ".[dev]"
)

echo "Setting up agent virtualenv..."
(
  cd "${PROJECT_ROOT}/agent"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e ".[dev]"
)

echo "Checking Flutter..."
if [ -x "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" ]; then
  "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" --version
  (
    cd "${PROJECT_ROOT}/mobile"
    "${PROJECT_ROOT}/.flutter-sdk/flutter/bin/flutter" pub get
  )
else
  yellow "Flutter SDK not found at ${PROJECT_ROOT}/.flutter-sdk/flutter."
  yellow "Install Flutter manually or use the CI workflow for Flutter validation."
fi

green "Setup complete. Run ./scripts/test-all.sh to validate."
