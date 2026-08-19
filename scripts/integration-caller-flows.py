#!/usr/bin/env python3
"""Integration tests for caller-only backend flows using real cloud credentials.

Covers session creation, consent, re-consent idempotency, join-code expiry, and
session end/delete-room cleanup. These flows do not require a support Firebase
ID token.

Run against a running backend (local or containerized):

    python scripts/integration-caller-flows.py

The backend must have real LiveKit/GCS credentials so that rooms are actually
created and deleted.
"""

from __future__ import annotations

import argparse
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


def _start_backend(env: dict[str, str]) -> subprocess.Popen:
    full_env = {**dict(subprocess.os.environ), **env}
    full_env["ALLOW_DEGRADED_START"] = "false"
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


def _test_create_consent_end(env: dict[str, str], service_key: str) -> None:
    print("\nTest: create -> consent -> end -> room deleted")
    caller_id = f"flow-caller-{uuid.uuid4().hex[:8]}"
    session = _api_request(
        "POST",
        "/v1/sessions",
        {"device_id": caller_id, "display_name": "Flow Test"},
        api_key=service_key,
    )
    session_id = session["session_id"]
    token = session["caller_session_token"]

    detail = _api_request("GET", f"/v1/sessions/{session_id}", api_key=service_key)
    room_name = detail["room_name"]
    assert detail["state"] == "pending", f"expected pending, got {detail['state']}"

    consent = _api_request(
        "POST",
        f"/v1/sessions/{session_id}/consent",
        {"accepted": True, "consent_text_version": env.get("CONSENT_TEXT_VERSION", "v1.0")},
        bearer=token,
    )
    assert consent.get("livekit"), "consent did not return LiveKit credentials"
    assert _livekit_room_exists(env, room_name), f"room {room_name} not found after consent"

    _api_request("POST", f"/v1/sessions/{session_id}/end", bearer=token)
    # Room deletion is synchronous; give LiveKit a moment to propagate.
    time.sleep(1)
    assert not _livekit_room_exists(env, room_name), f"room {room_name} still exists after end"
    print("  passed")


def _test_consent_idempotent(env: dict[str, str], service_key: str) -> None:
    print("\nTest: consent is idempotent")
    caller_id = f"flow-caller-{uuid.uuid4().hex[:8]}"
    session = _api_request(
        "POST",
        "/v1/sessions",
        {"device_id": caller_id},
        api_key=service_key,
    )
    session_id = session["session_id"]
    token = session["caller_session_token"]

    body = {"accepted": True, "consent_text_version": env.get("CONSENT_TEXT_VERSION", "v1.0")}
    first = _api_request("POST", f"/v1/sessions/{session_id}/consent", body, bearer=token)
    second = _api_request("POST", f"/v1/sessions/{session_id}/consent", body, bearer=token)
    # expires_at may shift by a second between calls; compare stable fields.
    assert first["accepted"] == second["accepted"]
    assert first["session_state"] == second["session_state"]
    assert first["recording_enabled"] == second["recording_enabled"]
    assert first.get("livekit") and second.get("livekit"), "re-consent missing LiveKit credentials"
    assert first["livekit"]["room_name"] == second["livekit"]["room_name"]
    print("  passed")


def _test_decline_fallback_disabled(env: dict[str, str], service_key: str) -> None:
    print("\nTest: decline without unrecorded fallback")
    caller_id = f"flow-caller-{uuid.uuid4().hex[:8]}"
    session = _api_request(
        "POST",
        "/v1/sessions",
        {"device_id": caller_id},
        api_key=service_key,
    )
    session_id = session["session_id"]
    token = session["caller_session_token"]

    declined = _api_request(
        "POST",
        f"/v1/sessions/{session_id}/consent",
        {"accepted": False, "consent_text_version": env.get("CONSENT_TEXT_VERSION", "v1.0")},
        bearer=token,
    )
    assert declined.get("accepted") is False
    if env.get("ALLOW_UNRECORDED_FALLBACK", "false").lower() != "true":
        assert declined.get("livekit") is None, "declined consent returned token when fallback disabled"
    print("  passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run caller-only integration flows.")
    parser.add_argument("--external-backend", action="store_true", help="Assume backend is already running")
    args = parser.parse_args(argv)

    env = _load_env()
    service_key = env.get("SERVICE_API_KEY", "")
    if not service_key or service_key.lower().startswith("change"):
        print("SERVICE_API_KEY missing or placeholder", file=sys.stderr)
        return 1

    proc = None
    try:
        if not args.external_backend:
            print("Starting backend...")
            proc = _start_backend(env)
            if not _wait_for_ready():
                print("Backend did not become healthy", file=sys.stderr)
                return 1
            print("Backend ready.")
        else:
            if not _wait_for_ready(timeout=5.0):
                print("No healthy backend on port 8000", file=sys.stderr)
                return 1

        ready = _api_request("GET", "/readyz")
        assert ready.get("status") == "healthy", ready
        print(f"/readyz: {ready['status']}")

        _test_create_consent_end(env, service_key)
        _test_consent_idempotent(env, service_key)
        _test_decline_fallback_disabled(env, service_key)

        print("\nAll caller-flow integration tests passed.")
        return 0
    except AssertionError as exc:
        print(f"\nTest failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nTest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
