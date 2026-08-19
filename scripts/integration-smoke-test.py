#!/usr/bin/env python3
"""End-to-end smoke test using real cloud credentials.

Starts the backend with the local `.env`, creates a support session, verifies the
LiveKit room exists, verifies GCS is reachable, then deletes the session and room.

This test is intentionally kept out of the unit-test suite so it does not run on
every `pytest` invocation or in CI. Run it manually when credentials are present:

    python scripts/integration-smoke-test.py

Requirements:
    - Postgres running and reachable via DATABASE_URL in backend/.env
    - LiveKit Cloud credentials in backend/.env
    - GCS service-account credentials in backend/.env
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request
import uuid


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND_ENV = PROJECT_ROOT / "backend" / ".env"
BACKEND_VENV_PYTHON = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in BACKEND_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _api_request(
    method: str,
    path: str,
    body: dict | None = None,
    api_key: str | None = None,
    bearer: str | None = None,
) -> dict:
    url = f"http://127.0.0.1:8000{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif api_key:
        headers["X-Service-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


def _wait_for_ready(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=2) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "healthy":
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_backend() -> subprocess.Popen:
    env = _load_env()
    env["ALLOW_DEGRADED_START"] = "false"
    full_env = {**dict(subprocess.os.environ), **env}
    full_env["PYTHONPATH"] = str(PROJECT_ROOT / "backend")
    proc = subprocess.Popen(
        [str(BACKEND_VENV_PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=str(PROJECT_ROOT / "backend"),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _livekit_room_exists(env: dict[str, str], room_name: str) -> bool:
    script = f'''
import asyncio, sys, livekit.api as api
from dotenv import load_dotenv
load_dotenv("{BACKEND_ENV}")
async def main():
    lk = api.LiveKitAPI()
    resp = await lk.room.list_rooms(api.ListRoomsRequest(names=["{room_name}"]))
    await lk.aclose()
    print("EXISTS" if resp.rooms else "MISSING")
asyncio.run(main())
'''
    proc = subprocess.run(
        [str(BACKEND_VENV_PYTHON), "-"],
        input=script,
        text=True,
        capture_output=True,
        cwd=str(PROJECT_ROOT / "backend"),
        env={**dict(subprocess.os.environ), "PYTHONPATH": str(PROJECT_ROOT / "backend")},
    )
    return "EXISTS" in (proc.stdout + proc.stderr)


def _delete_livekit_room(env: dict[str, str], room_name: str) -> None:
    script = f'''
import asyncio, sys, livekit.api as api
from dotenv import load_dotenv
load_dotenv("{BACKEND_ENV}")
async def main():
    lk = api.LiveKitAPI()
    try:
        await lk.room.delete_room(api.DeleteRoomRequest(room="{room_name}"))
    except Exception:
        pass
    await lk.aclose()
asyncio.run(main())
'''
    subprocess.run(
        [str(BACKEND_VENV_PYTHON), "-"],
        input=script,
        text=True,
        capture_output=True,
        cwd=str(PROJECT_ROOT / "backend"),
        env={**dict(subprocess.os.environ), "PYTHONPATH": str(PROJECT_ROOT / "backend")},
    )


def _gcs_reachable(env: dict[str, str]) -> bool:
    script = '''
import os, sys, base64, json
from dotenv import load_dotenv
from google.cloud import storage
load_dotenv("''' + str(BACKEND_ENV) + '''")
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
    proc = subprocess.run(
        [str(BACKEND_VENV_PYTHON), "-"],
        input=script,
        text=True,
        capture_output=True,
        cwd=str(PROJECT_ROOT / "backend"),
        env={**dict(subprocess.os.environ), "PYTHONPATH": str(PROJECT_ROOT / "backend")},
    )
    return (proc.stdout + proc.stderr).strip().endswith("OK")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an end-to-end smoke test with real cloud credentials.")
    parser.add_argument(
        "--keep-backend",
        action="store_true",
        help="Do not stop the backend subprocess after the test (useful for inspection)",
    )
    parser.add_argument(
        "--external-backend",
        action="store_true",
        help="Assume a backend is already running on http://127.0.0.1:8000",
    )
    args = parser.parse_args(argv)

    env = _load_env()
    service_key = env.get("SERVICE_API_KEY")
    if not service_key or service_key.lower().startswith("change"):
        print("SERVICE_API_KEY is missing or looks like a placeholder.", file=sys.stderr)
        return 1

    proc = None
    try:
        if not args.external_backend:
            print("Starting backend...")
            proc = _start_backend()
            if not _wait_for_ready():
                print("Backend did not become healthy in time.", file=sys.stderr)
                return 1
            print("Backend ready.")
        else:
            if not _wait_for_ready(timeout=5.0):
                print("No healthy backend found on http://127.0.0.1:8000", file=sys.stderr)
                return 1
            print("Using existing backend.")

        print("Checking /readyz...")
        ready = _api_request("GET", "/readyz")
        assert ready.get("status") == "healthy", ready
        print(f"  /readyz: {ready['status']}")

        print("Checking GCS connectivity...")
        assert _gcs_reachable(env), "GCS not reachable"
        print("  GCS: ok")

        caller_id = f"smoke-caller-{uuid.uuid4().hex[:8]}"
        print(f"Creating session for caller {caller_id}...")
        session = _api_request(
            "POST",
            "/v1/sessions",
            {"device_id": caller_id, "display_name": "Smoke Test"},
            api_key=service_key,
        )
        session_id = session["session_id"]
        print(f"  session: {session_id}")

        print("Fetching session detail for room name...")
        detail = _api_request("GET", f"/v1/sessions/{session_id}", api_key=service_key)
        room_name = detail["room_name"]
        print(f"  room:    {room_name}")

        caller_token = session["caller_session_token"]

        print("Recording consent...")
        consent = _api_request(
            "POST",
            f"/v1/sessions/{session_id}/consent",
            {"accepted": True, "consent_text_version": env.get("CONSENT_TEXT_VERSION", "v1.0")},
            bearer=caller_token,
        )
        assert consent.get("livekit", {}).get("token"), "Consent did not return LiveKit credentials"
        print("  consent: ok")

        print("Verifying LiveKit room exists...")
        assert _livekit_room_exists(env, room_name), f"Room {room_name} not found in LiveKit"
        print("  LiveKit room: ok")

        print("Ending session...")
        _api_request("POST", f"/v1/sessions/{session_id}/end", bearer=caller_token)
        print("  session ended")

        print("Cleaning up LiveKit room...")
        _delete_livekit_room(env, room_name)
        print("  room deleted")

        print("\nSmoke test passed.")
        return 0
    except AssertionError as exc:
        print(f"\nSmoke test failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nSmoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if proc is not None and not args.keep_backend:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
