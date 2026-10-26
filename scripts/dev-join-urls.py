#!/usr/bin/env python3
"""Mint browser join URLs for a fresh dev session on the local backend.

Creates a caller session (consent recorded) and a support token via the
internal test endpoint, then prints ``/support-web/`` URLs for both roles so a
human can join the same LiveKit room from a browser. The page is served
through the public tunnel host; the API calls go straight to the local
backend, so this must run on the backend host.

Usage:
  python3 scripts/dev-join-urls.py [--public-host remote-support.lgitech.net]

Requires the local backend on 127.0.0.1:8000 (scripts/dev-start.sh) and
ALLOW_TEST_ENDPOINTS=true. The URLs are valid for LIVEKIT_TOKEN_TTL_SECONDS
(default 15 min), and the session for JOIN_CODE_TTL_SECONDS after creation.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
import uuid
from pathlib import Path

BACKEND = "http://127.0.0.1:8000"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (PROJECT_ROOT / "backend" / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _post(
    path: str, payload: dict, headers: dict[str, str] | None = None
) -> dict:
    req = urllib.request.Request(
        BACKEND + path, data=json.dumps(payload).encode(), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _join_link(token: str, ws_url: str, identity: str, service_key: str) -> str:
    """Store the token behind a short redirect code for human sharing."""
    body = _post(
        "/internal/test/join-link",
        {"token": token, "ws_url": ws_url, "identity": identity},
        {"X-Service-Key": service_key},
    )
    return body["path"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-host", default="remote-support.lgitech.net")
    parser.add_argument("--name", default="Browser Caller")
    args = parser.parse_args()

    env = _load_env()
    service_key = env.get("SERVICE_API_KEY", "")
    if not service_key or service_key.startswith("change"):
        print("SERVICE_API_KEY missing/placeholder in backend/.env")
        return 1
    ws_url_fallback = env.get("LIVEKIT_URL", "")

    created = _post(
        "/v1/sessions",
        {
            "device_id": f"dev-{uuid.uuid4().hex[:8]}",
            "display_name": args.name,
            "locale": "en-US",
        },
    )
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    consent = _post(
        f"/v1/sessions/{session_id}/consent",
        {"accepted": True, "consent_text_version": env.get("CONSENT_TEXT_VERSION", "v1.0")},
        {"Authorization": f"Bearer {caller_token}"},
    )
    # The LiveKit token for the browser lives in the consent response;
    # caller_session_token is the HTTP credential, not a LiveKit token.
    caller = consent["livekit"]
    support = _post(
        "/internal/test/support-token",
        {"session_id": session_id},
        {"X-Service-Key": service_key},
    )
    ws_url = caller.get("ws_url") or support.get("ws_url") or ws_url_fallback

    caller_path = _join_link(caller["token"], ws_url, "browser-caller", service_key)
    support_path = _join_link(support["token"], ws_url, "browser-support", service_key)

    print(f"session:  {session_id}")
    join_code = created.get("join_code")
    if join_code:
        print(f"code:     {join_code}")
    print()
    print(f"CALLER :  https://{args.public_host}{caller_path}")
    print(f"SUPPORT:  https://{args.public_host}{support_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
