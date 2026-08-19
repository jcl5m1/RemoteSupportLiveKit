#!/usr/bin/env python3
"""Validate backend and agent environment files for local dev or production.

Reports missing required variables, suspicious placeholders, and whether the
Postgres database is reachable. Does not modify any files.

Examples:
    python scripts/check-env.py
    python scripts/check-env.py --prod
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import socket
import subprocess
import sys
import urllib.parse


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

BACKEND_ENV = PROJECT_ROOT / "backend" / ".env"
AGENT_ENV = PROJECT_ROOT / "agent" / ".env"

# Variables required in every mode (degraded or production).
BACKEND_REQUIRED_ALWAYS = {
    "DATABASE_URL",
    "CALLER_JWT_SECRET",
    "SERVICE_API_KEY",
    "FIREBASE_PROJECT_ID",
}

# Variables required for production (non-degraded) mode.
BACKEND_REQUIRED_PROD = {
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "GCS_BUCKET",
    "GCP_CREDENTIALS_B64",
}

AGENT_REQUIRED_ALWAYS = {
    "BACKEND_URL",
    "SERVICE_API_KEY",
}

AGENT_REQUIRED_PROD = {
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
}

PLACEHOLDER_PATTERNS = [
    re.compile(r"^\s*$"),  # empty
    re.compile(r"change[-_]?me", re.IGNORECASE),
    re.compile(r"your-", re.IGNORECASE),
    re.compile(r"example\.com", re.IGNORECASE),
    re.compile(r"^example$", re.IGNORECASE),
]


def _parse_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _is_placeholder(value: str) -> bool:
    return any(p.search(value) for p in PLACEHOLDER_PATTERNS)


def _check_required(
    label: str,
    env: dict[str, str],
    required: set[str],
) -> list[str]:
    missing = []
    for key in sorted(required):
        value = env.get(key)
        if value is None or value == "":
            missing.append(f"{label}: {key} is missing or empty")
        elif _is_placeholder(value):
            missing.append(f"{label}: {key} looks like a placeholder ({value!r})")
    return missing


def _check_url(label: str, env: dict[str, str], key: str, schemes: set[str]) -> list[str]:
    value = env.get(key)
    if not value:
        return []
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in schemes:
        return [f"{label}: {key}={value!r} has unexpected scheme (expected one of {schemes})"]
    return []


def _check_support_allowlist(label: str, env: dict[str, str]) -> list[str]:
    domains = env.get("SUPPORT_ALLOWED_DOMAINS", "").strip()
    emails = env.get("SUPPORT_ALLOWED_EMAILS", "").strip()
    if not domains and not emails:
        return [f"{label}: SUPPORT_ALLOWED_DOMAINS or SUPPORT_ALLOWED_EMAILS must be set"]
    return []


def _check_gcp_credentials(label: str, env: dict[str, str]) -> list[str]:
    value = env.get("GCP_CREDENTIALS_B64", "").strip()
    if not value:
        return []
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", value):
        return [f"{label}: GCP_CREDENTIALS_B64 contains non-base64 characters"]
    # Padding check: valid base64 length is a multiple of 4 (with padding).
    if len(value) % 4 != 0:
        return [f"{label}: GCP_CREDENTIALS_B64 length is not valid base64"]
    return []


def _check_postgres(env: dict[str, str]) -> list[str]:
    url = env.get("DATABASE_URL", "")
    if not url:
        return []
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg", "postgres"}:
        return [f"Postgres: DATABASE_URL scheme {parsed.scheme!r} is unexpected"]

    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError as exc:
        return [f"Postgres: cannot connect to {host}:{port} ({exc})"]
    return []


def _run_backend_venv_python(script: str, env_path: pathlib.Path) -> tuple[int, str]:
    """Run a Python snippet inside the backend virtualenv with the given .env."""
    venv_python = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return 1, "backend venv python not found"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "backend")
    proc = subprocess.run(
        [str(venv_python), "-", str(env_path)],
        input=script,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(PROJECT_ROOT / "backend"),
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _check_livekit(env: dict[str, str]) -> list[str]:
    script = '''
import asyncio, sys, livekit.api as api
from dotenv import load_dotenv
load_dotenv(sys.argv[1])
async def main():
    try:
        lk = api.LiveKitAPI()
        await lk.room.list_rooms(api.ListRoomsRequest())
        await lk.aclose()
        print("OK")
    except Exception as e:
        print(f"ERR: {type(e).__name__}: {e}")
asyncio.run(main())
'''
    env_path = PROJECT_ROOT / "backend" / ".env"
    rc, output = _run_backend_venv_python(script, env_path)
    if rc != 0 or not output.endswith("OK"):
        return [f"LiveKit: connectivity check failed ({output})"]
    return []


def _check_gcs(env: dict[str, str]) -> list[str]:
    script = '''
import os, sys, base64, json
from dotenv import load_dotenv
from google.cloud import storage
load_dotenv(sys.argv[1])
async def main():
    try:
        creds = json.loads(base64.b64decode(os.getenv("GCP_CREDENTIALS_B64")).decode())
        client = storage.Client.from_service_account_info(creds)
        bucket = client.bucket(os.getenv("GCS_BUCKET"))
        list(bucket.list_blobs(max_results=1))
        print("OK")
    except Exception as e:
        print(f"ERR: {type(e).__name__}: {e}")
import asyncio
asyncio.run(main())
'''
    env_path = PROJECT_ROOT / "backend" / ".env"
    rc, output = _run_backend_venv_python(script, env_path)
    if rc != 0 or not output.endswith("OK"):
        return [f"GCS: connectivity check failed ({output})"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate backend and agent environment files."
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Require LiveKit/GCS credentials (i.e. fail if degraded-mode placeholders are present)",
    )
    parser.add_argument(
        "--no-postgres",
        action="store_true",
        help="Skip the Postgres connectivity check",
    )
    parser.add_argument(
        "--check-cloud",
        action="store_true",
        help="Also verify LiveKit and GCS connectivity using backend/.venv",
    )
    args = parser.parse_args(argv)

    backend = _parse_env(BACKEND_ENV)
    agent = _parse_env(AGENT_ENV)

    issues: list[str] = []

    if not backend:
        issues.append(f"Backend: no .env file found at {BACKEND_ENV}")
    if not agent:
        issues.append(f"Agent: no .env file found at {AGENT_ENV}")

    mode = "production" if args.prod else "degraded"
    backend_prod = args.prod or backend.get("ALLOW_DEGRADED_START", "").lower() != "true"
    agent_prod = args.prod or agent.get("ALLOW_DEGRADED_START", "").lower() != "true"

    if backend:
        issues.extend(_check_required("backend", backend, BACKEND_REQUIRED_ALWAYS))
        if backend_prod:
            issues.extend(_check_required("backend", backend, BACKEND_REQUIRED_PROD))
        issues.extend(_check_url("backend", backend, "DATABASE_URL", {"postgresql", "postgresql+asyncpg", "postgres"}))
        issues.extend(_check_url("backend", backend, "LIVEKIT_URL", {"wss", "ws"}))
        issues.extend(_check_support_allowlist("backend", backend))
        if backend_prod:
            issues.extend(_check_gcp_credentials("backend", backend))
        if not args.no_postgres:
            issues.extend(_check_postgres(backend))
        if args.check_cloud:
            issues.extend(_check_livekit(backend))
            issues.extend(_check_gcs(backend))

    if agent:
        issues.extend(_check_required("agent", agent, AGENT_REQUIRED_ALWAYS))
        if agent_prod:
            issues.extend(_check_required("agent", agent, AGENT_REQUIRED_PROD))
        issues.extend(_check_url("agent", agent, "BACKEND_URL", {"http", "https"}))
        issues.extend(_check_url("agent", agent, "LIVEKIT_URL", {"wss", "ws"}))

    print(f"Environment check mode: {mode}")
    print(f"  backend .env: {BACKEND_ENV}")
    print(f"  agent .env:   {AGENT_ENV}")

    if issues:
        print(f"\nFound {len(issues)} issue(s):")
        for issue in issues:
            print(f"  - {issue}")
        print("\nFix the issues above, then re-run this script.")
        return 1

    print("\nAll required environment variables are present and look valid.")
    if not args.prod:
        print("Run with --prod to also validate LiveKit/GCS credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
