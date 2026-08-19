#!/usr/bin/env bash
# Start the local development stack (Postgres + backend + agent) without Docker.
# This runs each service in the background; press Ctrl-C to stop all of them.
# Cloud services (LiveKit, GCS) will be stubbed if ALLOW_DEGRADED_START=true.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${PROJECT_ROOT}/backend"
AGENT_DIR="${PROJECT_ROOT}/agent"

# Local development defaults. Cloud services (LiveKit, GCS) will be stubbed if
# ALLOW_DEGRADED_START=true and credentials are missing.
export ALLOW_DEGRADED_START="${ALLOW_DEGRADED_START:-true}"
export ENVIRONMENT="${ENVIRONMENT:-development}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

echo "Starting local dev stack. If LiveKit/GCS credentials are present in"
echo "backend/.env or agent/.env, the services will connect to real cloud"
echo "endpoints; otherwise they run in degraded mode."
echo ""

BACKEND_PID=""
AGENT_PID=""

cleanup() {
  echo ""
  echo "Shutting down services..."
  if [ -n "${BACKEND_PID}" ]; then
    kill "${BACKEND_PID}" 2>/dev/null || true
    pkill -P "${BACKEND_PID}" 2>/dev/null || true
  fi
  if [ -n "${AGENT_PID}" ]; then
    kill "${AGENT_PID}" 2>/dev/null || true
    pkill -P "${AGENT_PID}" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  echo "Stopped."
}
trap cleanup INT TERM EXIT

echo "=== verifying Postgres ==="
if ! (cd "${BACKEND_DIR}" && .venv/bin/python - <<'PY'
import asyncio, os, sys
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
async def main():
    url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://rs:rs@localhost:5432/remote_support")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        print("Postgres OK")
    finally:
        await engine.dispose()
asyncio.run(main())
PY
); then
  echo "Postgres is not reachable. Start it first, e.g.:"
  echo "  brew services start postgresql@16"
  exit 1
fi

echo "=== starting backend ==="
(
  cd "${BACKEND_DIR}"
  exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
) &
BACKEND_PID=$!

echo "=== waiting for backend /healthz ==="
for i in {1..30}; do
  if curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
    echo "backend healthy"
    break
  fi
  sleep 1
done

if ! curl -fsS http://localhost:8000/healthz >/dev/null 2>&1; then
  echo "backend failed to start"
  exit 1
fi

echo "=== starting agent ==="
(
  cd "${AGENT_DIR}"
  exec .venv/bin/python -m agent.main start
) &
AGENT_PID=$!

echo "=== local stack running ==="
echo "  backend: http://localhost:8000"
echo "  health:  http://localhost:8000/healthz"
echo "  ready:   http://localhost:8000/readyz"
echo "  metrics: http://localhost:8000/metrics"
echo "Press Ctrl-C to stop."

wait
