"""Two-web-client automated A/V regression test.

Requires:
  - Backend running with ALLOW_TEST_ENDPOINTS=true
  - SERVICE_API_KEY and BACKEND_URL in the environment or tests/e2e/.env
  - Playwright browsers installed (``playwright install chromium``)

The test exercises:
  1. Caller session creation + consent
  2. Internal support-token endpoint
  3. Two browser tabs joining the same LiveKit room with synthetic A/V
     (canvas clock video + tone/speech audio)
  4. Bidirectional video/audio track publication and subscription
  5. Network disconnect/reconnect survival
  6. Recording egress to GCS and signed download URLs
  7. Transcript capture (agent greeting, and caller/agent dialogue when speech
     audio is enabled)
  8. Session teardown and room cleanup
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import requests
from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, expect

# Allow running from repo root or tests/e2e/
_dotenv_paths = [
    os.path.join(os.path.dirname(__file__), ".env"),
    os.path.join(os.path.dirname(__file__), "..", ".env"),
    os.path.join(os.path.dirname(__file__), "..", "..", "backend", ".env"),
]
for _p in _dotenv_paths:
    if os.path.exists(_p):
        load_dotenv(_p)

BACKEND_URL = os.environ.get("BACKEND_URL", "https://remotesupport.lgitech.net").rstrip("/")
SERVICE_API_KEY = os.environ.get("SERVICE_API_KEY", "")
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
GCP_CREDENTIALS_B64 = os.environ.get("GCP_CREDENTIALS_B64", "")

if not SERVICE_API_KEY:
    raise RuntimeError("SERVICE_API_KEY must be set in the environment")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SPEECH_WAV = FIXTURES_DIR / "caller_prompt.wav"


class HarnessClient:
    """Thin HTTP client around the backend API used by the test harness."""

    def __init__(self, base_url: str, service_key: str) -> None:
        self.base_url = base_url
        self.service_key = service_key
        self.session = requests.Session()

    def create_session(self, device_id: str | None = None) -> dict[str, Any]:
        payload = {
            "device_id": device_id or f"test-{uuid.uuid4().hex[:8]}",
            "display_name": "Test Caller",
            "locale": "en-US",
        }
        r = self.session.post(f"{self.base_url}/v1/sessions", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def record_consent(self, session_id: str, caller_token: str) -> dict[str, Any]:
        r = self.session.post(
            f"{self.base_url}/v1/sessions/{session_id}/consent",
            headers={"Authorization": f"Bearer {caller_token}"},
            json={"accepted": True, "consent_text_version": "v1.0"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def support_token(self, session_id: str) -> dict[str, Any]:
        r = self.session.post(
            f"{self.base_url}/internal/test/support-token",
            headers={"X-Service-Key": self.service_key},
            json={"session_id": session_id},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_transcript(
        self,
        session_id: str,
        since_ms: int = 0,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"since_ms": since_ms, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = self.session.get(
            f"{self.base_url}/v1/sessions/{session_id}/transcript",
            headers={"X-Service-Key": self.service_key},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def list_recordings(self, session_id: str) -> dict[str, Any]:
        r = self.session.get(
            f"{self.base_url}/internal/test/{session_id}/recordings",
            headers={"X-Service-Key": self.service_key},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def end_session(self, session_id: str, caller_token: str) -> None:
        r = self.session.post(
            f"{self.base_url}/v1/sessions/{session_id}/end",
            headers={"Authorization": f"Bearer {caller_token}"},
            timeout=30,
        )
        r.raise_for_status()

    def get_session(self, session_id: str, caller_token: str) -> dict[str, Any]:
        r = self.session.get(
            f"{self.base_url}/v1/sessions/{session_id}",
            headers={"Authorization": f"Bearer {caller_token}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def all_utterances(self, session_id: str) -> list[dict[str, Any]]:
        """Fetch every utterance across cursor pages."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.get_transcript(session_id, cursor=cursor)
            out.extend(page.get("utterances", []))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return out


@pytest.fixture(scope="session")
def client() -> HarnessClient:
    return HarnessClient(BACKEND_URL, SERVICE_API_KEY)


@pytest.fixture(scope="session")
def audio_server() -> str:
    """Serve tests/e2e/fixtures/ on a local port so the browser can fetch WAV files."""
    if not SPEECH_WAV.exists():
        pytest.skip(f"Speech fixture not found: {SPEECH_WAV}")

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

        def log_message(self, format, *args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/caller_prompt.wav"
    yield url
    server.shutdown()


def _open_support_web(
    page: Page,
    credentials: dict[str, Any],
    identity: str,
    audio_url: str | None = None,
    tone_hz: int = 440,
) -> None:
    """Navigate a Playwright page to the web client with the given token."""
    ws_url = credentials.get("ws_url") or LIVEKIT_URL
    token = credentials["token"]
    params: dict[str, str] = {
        "token": token,
        "ws_url": ws_url,
        "identity": identity,
        "audio_tone_hz": str(tone_hz),
    }
    if audio_url:
        params["audio_url"] = audio_url
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BACKEND_URL}/support-web/?{qs}"
    page.on("console", lambda msg: print(f"[{identity}] {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: print(f"[{identity}] pageerror: {err}"))
    page.goto(url)
    # Prime the AudioContext so synthetic audio actually produces frames in
    # headless Chromium (autoplay policy is disabled via launch args, but a
    # click + explicit resume make this robust across versions).
    try:
        page.click("body", timeout=5000)
    except Exception:
        pass
    page.evaluate("async () => { if (window.testActions && window.testActions.resumeAudio) await window.testActions.resumeAudio(); }")


def _wait_for_state(page: Page, key: str, timeout_ms: int = 30000) -> None:
    """Poll window.testState until the boolean flag becomes true."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        value = page.evaluate(f"() => window.testState && window.testState.{key}")
        if value:
            return
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for window.testState.{key}")


def _wait_for_remote_video_playing(page: Page, timeout_ms: int = 30000) -> None:
    """Wait until the remote <video> element is playing with non-zero dimensions."""
    video = page.locator("#remoteVideo")
    expect(video).to_be_attached(timeout=timeout_ms)
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        ready = page.evaluate(
            """() => {
                const v = document.getElementById('remoteVideo');
                return v && v.readyState >= 2 && v.videoWidth > 0 && v.videoHeight > 0;
            }"""
        )
        if ready:
            return
        time.sleep(0.25)
    raise AssertionError("Remote video never started playing")


def _get_bytes_received(page: Page) -> dict[str, int]:
    """Return {audio, video} bytes received on the current page."""
    return page.evaluate(
        """async () => {
            const room = window.testState ? window.testState.room : null;
            if (!room || !room.engine) return { audio: 0, video: 0 };
            const pcm = room.engine.pcManager;
            const conn = pcm ? (pcm.subscriber || pcm.publisher) : null;
            const pc = conn && conn._pc ? conn._pc : conn;
            if (!pc || !pc.getStats) return { audio: 0, video: 0 };
            const stats = await pc.getStats();
            let audio = 0;
            let video = 0;
            stats.forEach(s => {
                if (s.type === 'inbound-rtp') {
                    const kind = s.mediaType || s.kind;
                    if (kind === 'audio') audio += s.bytesReceived || 0;
                    if (kind === 'video') video += s.bytesReceived || 0;
                }
            });
            return { audio, video };
        }"""
    )


def _assert_bytes_increase(page: Page, label: str) -> None:
    """Assert that inbound audio + video bytes increase over a short window."""
    before = _get_bytes_received(page)
    time.sleep(3)
    after = _get_bytes_received(page)
    assert after["audio"] > before["audio"], f"{label}: audio bytes did not increase"
    assert after["video"] > before["video"], f"{label}: video bytes did not increase"


def _agent_joined(page: Page, timeout_ms: int = 30000) -> bool:
    """Return true when an agent participant is visible to the page.

    LiveKit agents assigns the identity ``agent-{job_id}``, so we match the
    prefix rather than an exact name.
    """
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        has_agent = page.evaluate(
            """() => {
                const room = window.testState ? window.testState.room : null;
                if (!room) return false;
                for (const p of room.remoteParticipants.values()) {
                    if (p.identity.startsWith('agent-')) return true;
                }
                return false;
            }"""
        )
        if has_agent:
            return True
        time.sleep(0.25)
    return False


def test_two_web_clients(client: HarnessClient, browser: Browser) -> None:
    """Baseline two-web-client A/V flow."""
    created = client.create_session()
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    caller_ctx = None
    support_ctx = None

    try:
        consent = client.record_consent(session_id, caller_token)
        caller_creds = consent["livekit"]
        support_creds = client.support_token(session_id)

        caller_ctx = browser.new_context()
        support_ctx = browser.new_context()

        caller_page = caller_ctx.new_page()
        support_page = support_ctx.new_page()

        _open_support_web(caller_page, caller_creds, identity="caller")
        _open_support_web(support_page, support_creds, identity="support")

        _wait_for_state(caller_page, "connected")
        _wait_for_state(support_page, "connected")

        _wait_for_state(caller_page, "remoteVideoReady")
        _wait_for_state(support_page, "remoteVideoReady")
        _wait_for_remote_video_playing(caller_page)
        _wait_for_remote_video_playing(support_page)

        _wait_for_state(caller_page, "remoteAudioReady")
        _wait_for_state(support_page, "remoteAudioReady")

        _assert_bytes_increase(caller_page, "caller")
        _assert_bytes_increase(support_page, "support")

    finally:
        for ctx in (caller_ctx, support_ctx):
            if ctx is not None:
                ctx.close()
        try:
            client.end_session(session_id, caller_token)
        except requests.HTTPError as exc:
            print(f"end_session failed (session may already be closed): {exc}")

    time.sleep(2)
    detail = client.get_session(session_id, caller_token)
    assert detail["state"] in ("completed", "active"), detail


def test_lifecycle_reconnect(client: HarnessClient, browser: Browser) -> None:
    """Disconnect one tab from the network and confirm it reconnects."""
    created = client.create_session()
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    caller_ctx = None
    support_ctx = None

    try:
        consent = client.record_consent(session_id, caller_token)
        caller_creds = consent["livekit"]
        support_creds = client.support_token(session_id)

        caller_ctx = browser.new_context()
        support_ctx = browser.new_context()

        caller_page = caller_ctx.new_page()
        support_page = support_ctx.new_page()

        _open_support_web(caller_page, caller_creds, identity="caller")
        _open_support_web(support_page, support_creds, identity="support")

        _wait_for_state(caller_page, "connected")
        _wait_for_state(support_page, "connected")
        _wait_for_state(caller_page, "remoteVideoReady")

        # Capture reconnect count before going offline.
        before = caller_page.evaluate("() => window.testState.reconnectCount")

        # Drop caller's network for 5 s.
        caller_page.context.set_offline(True)
        time.sleep(5)
        caller_page.context.set_offline(False)

        # Wait for reconnect.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            after = caller_page.evaluate("() => window.testState.reconnectCount")
            state = caller_page.evaluate("() => window.testState.connectionState")
            if after > before and state == "connected":
                break
            time.sleep(0.25)
        else:
            raise AssertionError("Caller did not reconnect after network drop")

        # Media should resume.
        _wait_for_state(caller_page, "remoteVideoReady")
        _assert_bytes_increase(caller_page, "caller-after-reconnect")

    finally:
        for ctx in (caller_ctx, support_ctx):
            if ctx is not None:
                ctx.close()
        try:
            client.end_session(session_id, caller_token)
        except requests.HTTPError as exc:
            print(f"end_session failed: {exc}")


def test_recordings_and_transcript_after_session_end(
    client: HarnessClient, browser: Browser
) -> None:
    """Run a short call, end it, then verify recordings and transcript exports exist.

    LiveKit delivers ``track_published``, ``egress_ended`` and ``room_finished``
    webhooks to the backend URL. When that URL is localhost, the cloud service
    cannot reach it, so egress never starts and the post-room transcript export
    is never triggered. This test is therefore skipped for local development and
    should be run against the deployed backend (e.g. remotesupport.lgitech.net).
    """
    parsed = urlparse(BACKEND_URL)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        pytest.skip("Recording egress requires LiveKit webhooks; cannot deliver to localhost")

    created = client.create_session()
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    caller_ctx = None
    support_ctx = None

    try:
        consent = client.record_consent(session_id, caller_token)
        caller_creds = consent["livekit"]
        support_creds = client.support_token(session_id)

        caller_ctx = browser.new_context()
        support_ctx = browser.new_context()

        caller_page = caller_ctx.new_page()
        support_page = support_ctx.new_page()

        _open_support_web(caller_page, caller_creds, identity="caller")
        _open_support_web(support_page, support_creds, identity="support")

        _wait_for_state(caller_page, "connected")
        _wait_for_state(support_page, "connected")
        _wait_for_state(caller_page, "remoteVideoReady")
        _wait_for_state(support_page, "remoteVideoReady")

        # Let tracks publish and egress start.
        time.sleep(10)

        # End the session via API.
        client.end_session(session_id, caller_token)

        # Wait for egress completion and transcript export.
        # This can take 30-90 s after room deletion.
        recordings: dict[str, Any] | None = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            recordings = client.list_recordings(session_id)
            complete = [r for r in recordings.get("recordings", []) if r["state"] == "complete"]
            transcript = recordings.get("transcript")
            if len(complete) >= 5 and transcript:
                break
            time.sleep(5)
        else:
            raise AssertionError(
                f"Recordings did not complete in time: {json.dumps(recordings, indent=2, default=str)}"
            )

        assert recordings is not None
        kinds = {r["kind"] for r in recordings["recordings"]}
        assert "track_video" in kinds
        assert "track_audio" in kinds
        assert "room_composite" in kinds

        # Verify at least one signed URL is fetchable.
        video = next(r for r in recordings["recordings"] if r["kind"] == "room_composite")
        url = video["download_url"]
        head = requests.head(url, timeout=30)
        assert head.status_code == 200, f"Recording HEAD failed: {head.status_code}"
        assert int(head.headers.get("content-length", 0)) > 0, "Recording is empty"

        # Verify transcript exports are fetchable.
        for key in ("jsonl_url", "vtt_url", "txt_url"):
            url = recordings["transcript"][key]
            r = requests.get(url, timeout=30)
            assert r.status_code == 200, f"Transcript {key} failed: {r.status_code}"
            assert len(r.text) > 0, f"Transcript {key} is empty"

    finally:
        for ctx in (caller_ctx, support_ctx):
            if ctx is not None:
                ctx.close()
        try:
            client.end_session(session_id, caller_token)
        except requests.HTTPError:
            pass


def test_agent_joins_and_greets(client: HarnessClient, browser: Browser) -> None:
    """Confirm the AI agent joins the room after caller consent and produces a transcript utterance."""
    created = client.create_session()
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    caller_ctx = None
    support_ctx = None

    try:
        consent = client.record_consent(session_id, caller_token)
        caller_creds = consent["livekit"]
        support_creds = client.support_token(session_id)

        caller_ctx = browser.new_context()
        support_ctx = browser.new_context()

        caller_page = caller_ctx.new_page()
        support_page = support_ctx.new_page()

        _open_support_web(caller_page, caller_creds, identity="caller")
        _open_support_web(support_page, support_creds, identity="support")

        _wait_for_state(caller_page, "connected")

        # Agent should appear as a remote participant.
        assert _agent_joined(caller_page), "Agent did not join the caller's room"

        # Wait for the agent greeting to be transcribed.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            utterances = client.all_utterances(session_id)
            agent_greetings = [u for u in utterances if u["role"] == "agent" and u["source"] == "agent_llm"]
            if agent_greetings:
                break
            time.sleep(2)
        else:
            raise AssertionError("No agent_llm utterance found within timeout")

        assert agent_greetings, "Agent did not produce a greeting"
        print(f"Agent greeting: {agent_greetings[0]['text'][:120]}")

    finally:
        for ctx in (caller_ctx, support_ctx):
            if ctx is not None:
                ctx.close()
        try:
            client.end_session(session_id, caller_token)
        except requests.HTTPError:
            pass


def test_caller_speech_agent_response(
    client: HarnessClient, browser: Browser, audio_server: str
) -> None:
    """Play real speech audio from the caller and verify the agent transcribes and responds.

    This requires a speech fixture (tests/e2e/fixtures/caller_prompt.wav). On macOS
    it can be regenerated with ``say`` and ``ffmpeg``; see the fixtures README.

    Only the caller joins so the agent stays in SOLO mode and responds to any
    caller speech (the ASSISTED-mode direct-address gate is tested separately).
    """
    created = client.create_session()
    session_id = created["session_id"]
    caller_token = created["caller_session_token"]
    caller_ctx = None

    try:
        consent = client.record_consent(session_id, caller_token)
        caller_creds = consent["livekit"]

        caller_ctx = browser.new_context()
        caller_page = caller_ctx.new_page()

        _open_support_web(
            caller_page, caller_creds, identity="caller", audio_url=audio_server
        )

        _wait_for_state(caller_page, "connected")
        _wait_for_state(caller_page, "remoteAudioReady")

        # Wait for agent greeting, caller speech transcription, and agent reply.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            utterances = client.all_utterances(session_id)
            caller_speech = [u for u in utterances if u["role"] == "caller" and u["source"] == "agent_stt"]
            agent_replies = [u for u in utterances if u["role"] == "agent" and u["source"] == "agent_llm"]
            if caller_speech and len(agent_replies) >= 2:
                break
            time.sleep(3)
        else:
            utterances = client.all_utterances(session_id)
            raise AssertionError(
                f"Agent did not transcribe caller speech and respond. Utterances: {json.dumps(utterances, indent=2, default=str)}"
            )

        assert caller_speech, "Caller speech was not transcribed"
        assert len(agent_replies) >= 2, "Agent did not respond to caller speech"
        print(f"Caller heard: {caller_speech[0]['text'][:120]}")
        print(f"Agent reply: {agent_replies[-1]['text'][:120]}")

    finally:
        if caller_ctx is not None:
            caller_ctx.close()
        try:
            client.end_session(session_id, caller_token)
        except requests.HTTPError:
            pass
