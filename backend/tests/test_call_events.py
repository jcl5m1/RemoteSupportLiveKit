"""Interaction log for open calls: intake, server events, JSONL export."""

from __future__ import annotations

import json

import pytest

import app.routers.calls as calls_module
from app.services import call_events


class _Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = (data, content_type)


class _Bucket:
    def __init__(self, store):
        self.store = store

    def blob(self, name):
        return _Blob(self.store, name)


class _FakeGCS:
    def __init__(self):
        self.store = {}

    def bucket(self, name):
        return _Bucket(self.store)


class _Room:
    """Minimal LiveKit room service with a stable SID."""

    def __init__(self, api):
        self.api = api
        self.metadata = ""

    async def create_room(self, request):
        return self.api.Room(name=request.name, sid="RM_test123")

    async def list_rooms(self, request):
        return self.api.ListRoomsResponse(
            rooms=[
                self.api.Room(name=n, sid="RM_test123", metadata=self.metadata)
                for n in request.names
            ]
        )

    async def update_room_metadata(self, request):
        self.metadata = request.metadata

    async def delete_room(self, request):
        return None

    async def list_participants(self, request):
        return self.api.ListParticipantsResponse(participants=[])


class _Egress:
    def __init__(self, api):
        self.api = api

    async def list_egress(self, request):
        return self.api.ListEgressResponse(items=[])


@pytest.fixture
def call_env(app, monkeypatch):
    from livekit import api

    monkeypatch.setattr(calls_module.settings, "open_calls_enabled", True)
    monkeypatch.setattr(calls_module.settings, "open_call_recording_enabled", True)
    app.state.lkapi.room = _Room(api)
    app.state.lkapi.egress = _Egress(api)
    gcs = _FakeGCS()
    app.state.gcs_client = gcs
    call_events._sid_cache.clear()
    return gcs


async def _join(client, name):
    resp = await client.post("/v1/calls/team-sync/join", json={"display_name": name})
    return resp.json()["livekit"]["token"]


@pytest.mark.asyncio
async def test_events_are_stored_and_exported_beside_recordings(client, call_env):
    alice = await _join(client, "Alice")
    auth = {"Authorization": f"Bearer {alice}"}
    batch = {
        "events": [
            {"id": "e1", "type": "chat", "ts": 1761530000000, "data": {"text": "hello"}},
            {"id": "e2", "type": "pointer", "ts": 1761530001000,
             "data": {"target": "guest-bob-1", "x": 0.3, "y": 0.6, "color": "#E69F00"}},
        ]
    }
    resp = await client.post("/v1/calls/team-sync/events", json=batch, headers=auth)
    assert resp.status_code == 202 and resp.json() == {"stored": 2}
    # Retried batch: deduplicated by event id.
    resp = await client.post("/v1/calls/team-sync/events", json=batch, headers=auth)
    assert resp.json() == {"stored": 0}

    # Recording stop exports immediately, next to the recordings.
    await client.post("/v1/calls/team-sync/recording/start", headers=auth)
    await client.post("/v1/calls/team-sync/recording/stop", headers=auth)
    (path,) = [p for p in call_env.store if p.endswith("_events.jsonl")]
    assert path.startswith("calls/team-sync/") and "_RM_test123_" in path
    body, ctype = call_env.store[path]
    assert ctype == "application/x-ndjson"
    events = [json.loads(line) for line in body.splitlines()]
    assert [e["type"] for e in events] == [
        "join", "chat", "pointer", "recording_start", "recording_stop"
    ]
    chat = events[1]
    assert chat["identity"].startswith("guest-alice") and chat["name"] == "Alice"
    assert chat["data"] == {"text": "hello"}
    assert chat["client_ts"] == "2025-10-27T01:53:20.000+00:00"
    assert chat["ts"].endswith("+00:00") and chat["received_ts"].endswith("+00:00")
    # Ordered by when things happened (server clock).
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)

    # Ending the call logs it and re-exports the same file.
    await client.post("/v1/calls/team-sync/end", headers=auth)
    body, _ = call_env.store[path]
    assert json.loads(body.splitlines()[-1])["type"] == "call_end"


@pytest.mark.asyncio
async def test_event_intake_validates(client, call_env):
    alice = await _join(client, "Alice")
    auth = {"Authorization": f"Bearer {alice}"}
    resp = await client.post(
        "/v1/calls/team-sync/events", json={"events": [{"type": "nope"}]}, headers=auth
    )
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_event"
    big = {"events": [{"type": "chat", "data": {"text": "x" * 9000}}]}
    resp = await client.post("/v1/calls/team-sync/events", json=big, headers=auth)
    assert resp.status_code == 400
    resp = await client.post("/v1/calls/team-sync/events", json={"events": []})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_recordings_list_includes_event_logs(client, call_env, app):
    class _ListBlob:
        def __init__(self, name, size):
            self.name, self.size = name, size

        def generate_signed_url(self, **kw):
            return f"https://signed.example/{self.name}"

    class _ListBucket:
        def list_blobs(self, prefix):
            return [
                _ListBlob("calls/team-sync/20261027T010000Z_guest-alice-1a2b3c4d.mp4", 100),
                _ListBlob("calls/team-sync/20261027T010000Z_RM_test123_events.jsonl", 50),
                _ListBlob("calls/team-sync/EG_x.json", 10),
            ]

    app.state.gcs_client = type("C", (), {"bucket": lambda self, n: _ListBucket()})()
    body = (await client.get("/v1/calls/team-sync/recordings")).json()
    assert [r["name"] for r in body["recordings"]] == [
        "20261027T010000Z_guest-alice-1a2b3c4d.mp4"
    ]
    assert [log["name"] for log in body["event_logs"]] == [
        "20261027T010000Z_RM_test123_events.jsonl"
    ]
    assert body["event_logs"][0]["url"].startswith("https://signed.example/")



@pytest.mark.asyncio
async def test_event_time_corrects_batching_delay_and_clock_skew(client, call_env):
    """ts = client_ts + (server receive - batch sent_at): a sender whose clock
    is 1 h behind, reporting events 1.5 s old, still lands at the right time."""
    import time
    from datetime import datetime

    alice = await _join(client, "Alice")
    auth = {"Authorization": f"Bearer {alice}"}
    skew_ms = 3_600_000
    now_client = int(time.time() * 1000) - skew_ms
    batch = {
        "sent_at": now_client,
        "events": [
            {"id": "s1", "type": "mute", "ts": now_client - 1500, "data": {"muted": True}},
            {"id": "s2", "type": "mute", "ts": now_client - 500, "data": {"muted": False}},
        ],
    }
    await client.post("/v1/calls/team-sync/events", json=batch, headers=auth)
    await call_events.export_now(call_env, "team-sync", "RM_test123")
    (path,) = [p for p in call_env.store if p.endswith("_events.jsonl")]
    rows = [json.loads(line) for line in call_env.store[path][0].splitlines()]
    mutes = [r for r in rows if r["type"] == "mute"]
    received = datetime.fromisoformat(mutes[0]["received_ts"])
    for row, age_s in zip(mutes, (1.5, 0.5), strict=True):
        happened = datetime.fromisoformat(row["ts"])
        assert abs((received - happened).total_seconds() - age_s) < 0.2, row
