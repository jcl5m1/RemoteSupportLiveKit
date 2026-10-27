"""Open multi-party call endpoints."""

from __future__ import annotations

import pytest

import app.routers.calls as calls_module


@pytest.fixture
def open_calls(monkeypatch):
    monkeypatch.setattr(calls_module.settings, "open_calls_enabled", True)


@pytest.mark.asyncio
async def test_open_calls_disabled_by_default(client, monkeypatch):
    monkeypatch.setattr(calls_module.settings, "open_calls_enabled", False)
    resp = await client.post("/v1/calls/team-sync/join", json={})
    assert resp.status_code == 404
    resp = await client.get("/call/team-sync")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_root_serves_join_screen(client, open_calls):
    """No code: the join screen itself (client generates a code)."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "window.APP_CALL_ID = null;" in resp.text
    assert '<script src="/support-web/app.js' in resp.text
    # /call is a short alias for the entry point.
    resp = await client.get("/call")
    assert resp.status_code == 302 and resp.headers["location"] == "/"


@pytest.mark.asyncio
async def test_meeting_link_is_named_call_param(client, open_calls):
    """Canonical meeting link: /?call=<code>, served directly."""
    resp = await client.get("/?call=team-sync")
    assert resp.status_code == 200
    assert 'window.APP_CALL_ID = "team-sync";' in resp.text
    assert resp.headers["cache-control"] == "no-cache"
    # Non-canonical spellings and the path form redirect to it.
    for path in ("/?call=Team-Sync", "/call/team-sync", "/call/Team-Sync"):
        resp = await client.get(path)
        assert resp.status_code == 302, path
        assert resp.headers["location"] == "/?call=team-sync"


@pytest.mark.asyncio
async def test_support_session_link_uses_named_param(client):
    """/join/<id> -> /?session=<id>: same entry point, different parameter."""
    resp = await client.get("/join/abc-123")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?session=abc-123"


@pytest.mark.asyncio
async def test_join_open_call_multiple_guests(client, open_calls):
    identities = set()
    for name in ("Alice", "Bob", "Carol"):
        resp = await client.post("/v1/calls/team-sync/join", json={"display_name": name})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["call_id"] == "team-sync"
        assert body["livekit"]["room_name"] == "call_team-sync"
        assert body["livekit"]["identity"].startswith("guest-")
        identities.add(body["livekit"]["identity"])
    assert len(identities) == 3


@pytest.mark.asyncio
async def test_guest_token_is_scoped_to_call_room(client, open_calls):
    import jwt

    resp = await client.post("/v1/calls/team-sync/join", json={"display_name": "Al"})
    claims = jwt.decode(resp.json()["livekit"]["token"], options={"verify_signature": False})
    assert claims["video"]["room"] == "call_team-sync"
    assert not claims["video"].get("roomAdmin")
    assert claims["name"] == "Al"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["ab", "-abc", "has space", "x" * 65, "rs_123"])
async def test_join_rejects_invalid_call_id(client, open_calls, bad_id):
    resp = await client.post(f"/v1/calls/{bad_id}/join", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_call_id"


class _FakeCallRoom:
    """In-memory LiveKit room + egress service for recording tests."""

    def __init__(self) -> None:
        from livekit import api

        self.api = api
        self.metadata = ""
        self.participants = []
        self.egresses = {}  # egress_id -> (identity, filepath, active)
        self.max_concurrent: int | None = None
        self.deleted = False

    def publish(self, identity: str, sid: str, video: bool) -> None:
        api = self.api
        track = api.TrackInfo(
            sid=sid,
            type=api.TrackType.VIDEO if video else api.TrackType.AUDIO,
            source=api.TrackSource.CAMERA if video else api.TrackSource.MICROPHONE,
        )
        for p in self.participants:
            if p.identity == identity:
                p.tracks.append(track)
                return
        self.participants.append(api.ParticipantInfo(identity=identity, tracks=[track]))

    # room service
    async def create_room(self, request):
        return self.api.Room(name=request.name)

    async def list_rooms(self, request):
        if self.deleted:
            return self.api.ListRoomsResponse(rooms=[])
        return self.api.ListRoomsResponse(
            rooms=[
                self.api.Room(
                    name=n, metadata=self.metadata, num_participants=len(self.participants)
                )
                for n in request.names
            ]
        )

    async def update_room_metadata(self, request):
        if self.deleted:
            from livekit.api.twirp_client import ServerError

            raise ServerError("not_found", "requested room does not exist", status=404)
        self.metadata = request.metadata

    async def delete_room(self, request):
        self.deleted = True

    async def list_participants(self, request):
        return self.api.ListParticipantsResponse(participants=self.participants)

    # egress service
    async def start_participant_egress(self, request):
        if self.max_concurrent is not None and len(self.recorded()) >= self.max_concurrent:
            from livekit.api.twirp_client import ServerError

            raise ServerError("resource_exhausted", "concurrent egress limit", status=429)
        egress_id = f"EG_{len(self.egresses)}"
        (output,) = request.file_outputs
        self.egresses[egress_id] = (request.identity, output.filepath, True)
        return self.api.EgressInfo(egress_id=egress_id)

    async def stop_egress(self, request):
        identity, path, _ = self.egresses[request.egress_id]
        self.egresses[request.egress_id] = (identity, path, False)

    async def list_egress(self, request):
        api = self.api
        return api.ListEgressResponse(
            items=[
                api.EgressInfo(
                    egress_id=eid,
                    status=api.EgressStatus.EGRESS_ACTIVE,
                    participant=api.ParticipantEgressRequest(identity=identity),
                )
                for eid, (identity, _, active) in self.egresses.items()
                if active
            ]
        )

    def recorded(self) -> set[str]:
        return {identity for identity, _, active in self.egresses.values() if active}


@pytest.fixture
def call_room(app, open_calls, monkeypatch):
    monkeypatch.setattr(calls_module.settings, "open_call_recording_enabled", True)
    monkeypatch.setattr(calls_module.settings, "gcs_bucket", "test-bucket")
    monkeypatch.setattr(calls_module.settings, "gcp_credentials_b64", "e30=")
    fake = _FakeCallRoom()
    app.state.lkapi.room = fake
    app.state.lkapi.egress = fake
    return fake


async def _join(client, name: str) -> str:
    resp = await client.post("/v1/calls/team-sync/join", json={"display_name": name})
    assert resp.status_code == 200, resp.text
    assert resp.json()["recording_enabled"] is True
    return resp.json()["livekit"]["token"]


@pytest.mark.asyncio
async def test_recording_toggle_records_each_person_once(client, call_room):
    from app.services import call_recording

    alice = await _join(client, "Alice")
    bob = await _join(client, "Bob")
    call_room.publish("guest-alice-1", "TR_A_MIC", video=False)
    call_room.publish("guest-alice-1", "TR_A_CAM", video=True)
    call_room.publish("guest-bob-2", "TR_B_MIC", video=False)
    call_room.participants.append(call_room.api.ParticipantInfo(identity="guest-lurker-3"))
    auth = {"Authorization": f"Bearer {alice}"}

    info = (await client.get("/v1/calls/team-sync")).json()
    assert info["recording_active"] is False
    assert info["room_active"] is True and info["participant_count"] == 3
    resp = await client.post("/v1/calls/team-sync/recording/start", headers=auth)
    assert resp.status_code == 200
    assert resp.json() == {"recording": True, "started_by": "Alice"}
    # One egress per publishing person (mic+camera in one MP4); no media, no egress.
    assert call_room.recorded() == {"guest-alice-1", "guest-bob-2"}
    assert len(call_room.egresses) == 2
    paths = sorted(path for _, path, _ in call_room.egresses.values())
    assert all(p.startswith("calls/team-sync/") and p.endswith(".mp4") for p in paths)
    assert any(p.endswith("_guest-alice-1.mp4") for p in paths)
    assert (await client.get("/v1/calls/team-sync")).json()["recording_active"] is True

    # Starting again is idempotent; Bob's late camera needs no new egress, and a
    # late joiner is picked up by sync.
    await client.post("/v1/calls/team-sync/recording/start", headers=auth)
    call_room.publish("guest-bob-2", "TR_B_CAM", video=True)
    call_room.publish("guest-carol-4", "TR_C_MIC", video=False)
    resp = await client.post(
        "/v1/calls/team-sync/recording/sync", headers={"Authorization": f"Bearer {bob}"}
    )
    assert resp.json()["recording"] is True
    assert call_room.recorded() == {"guest-alice-1", "guest-bob-2", "guest-carol-4"}
    assert len(call_room.egresses) == 3

    # Anyone (here Bob) can stop it; every live egress is stopped.
    resp = await client.post(
        "/v1/calls/team-sync/recording/stop", headers={"Authorization": f"Bearer {bob}"}
    )
    assert resp.json() == {"recording": False, "started_by": None}
    assert call_room.recorded() == set()
    assert "team-sync" not in "".join(call_recording._loops)

    # With recording off, sync starts nothing.
    call_room.publish("guest-dan-5", "TR_D_MIC", video=False)
    await client.post("/v1/calls/team-sync/recording/sync", headers=auth)
    assert call_room.recorded() == set()


@pytest.mark.asyncio
async def test_recording_requires_token_for_this_call(client, call_room):
    resp = await client.post("/v1/calls/team-sync/recording/start")
    assert resp.status_code == 401
    resp = await client.post(
        "/v1/calls/team-sync/recording/start", headers={"Authorization": "Bearer junk"}
    )
    assert resp.status_code == 401

    other = (
        await client.post("/v1/calls/other-call/join", json={"display_name": "Eve"})
    ).json()["livekit"]["token"]
    resp = await client.post(
        "/v1/calls/team-sync/recording/start", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 403
    assert call_room.metadata == ""


@pytest.mark.asyncio
async def test_recording_disabled_by_setting(client, call_room, monkeypatch):
    monkeypatch.setattr(calls_module.settings, "open_call_recording_enabled", False)
    token = (await client.post("/v1/calls/team-sync/join", json={})).json()["livekit"]["token"]
    resp = await client.post(
        "/v1/calls/team-sync/recording/start", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "recording_disabled"


@pytest.mark.asyncio
async def test_recording_reports_people_skipped_by_egress_cap(client, call_room):
    import json

    from app.services import call_recording

    call_room.max_concurrent = 2
    token = await _join(client, "Alice")
    for i in range(3):
        call_room.publish(f"guest-p{i}-{i}", f"TR_{i}", video=False)
    auth = {"Authorization": f"Bearer {token}"}

    await client.post("/v1/calls/team-sync/recording/start", headers=auth)
    assert len(call_room.recorded()) == 2
    assert json.loads(call_room.metadata)["skipped"] == 1

    # A slot frees up: the next reconcile records the skipped person and clears it.
    call_room.max_concurrent = None
    await call_recording.reconcile(call_room_lkapi(client), "team-sync", "call_team-sync")
    assert call_room.recorded() == {"guest-p0-0", "guest-p1-1", "guest-p2-2"}
    assert json.loads(call_room.metadata)["skipped"] == 0
    await client.post("/v1/calls/team-sync/recording/stop", headers=auth)


def call_room_lkapi(client):
    return client._transport.app.state.lkapi


@pytest.mark.asyncio
async def test_served_under_base_path(app, open_calls):
    """Behind a shared hostname (e.g. gx10-645d.lgitech.net/livekit) both the
    prefixed and bare paths work, and pages/redirects keep the prefix."""
    import httpx

    from app.base_path import BasePathMiddleware

    wrapped = BasePathMiddleware(app, "/livekit")
    transport = httpx.ASGITransport(app=wrapped)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/livekit/")
        assert resp.status_code == 200
        assert 'window.APP_BASE = "/livekit";' in resp.text
        assert '<script src="/livekit/support-web/app.js' in resp.text

        resp = await c.get("/livekit/call")
        assert resp.headers["location"] == "/livekit/"

        resp = await c.get("/livekit/?call=team-sync")
        assert resp.status_code == 200
        assert 'window.APP_CALL_ID = "team-sync";' in resp.text
        resp = await c.get("/livekit/call/Team-Sync")
        assert resp.headers["location"] == "/livekit/?call=team-sync"
        assert (await c.get("/livekit/support-web/app.js")).status_code == 200

        resp = await c.post("/livekit/v1/calls/team-sync/join", json={"display_name": "A"})
        assert resp.status_code == 200
        assert (await c.get("/livekit/healthz")).json() == {"status": "ok"}

        # Bare paths still work (dev-remotesupport.lgitech.net).
        resp = await c.get("/?call=team-sync")
        assert resp.status_code == 200 and 'window.APP_BASE = "";' in resp.text
        # A different prefix-like path is not treated as the prefix.
        assert (await c.get("/livekitx/healthz")).status_code == 404


class _FakeBlob:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size

    def generate_signed_url(self, **kwargs):
        return f"https://signed.example/{self.name}"


class _FakeBucketWithBlobs:
    def __init__(self, blobs) -> None:
        self.blobs = blobs

    def list_blobs(self, prefix: str):
        return [b for b in self.blobs if b.name.startswith(prefix)]


@pytest.mark.asyncio
async def test_list_call_recordings_needs_only_the_link(app, client, open_calls):
    bucket = _FakeBucketWithBlobs(
        [
            _FakeBlob("calls/team-sync/20261026T053900Z_guest-bob-lee-77997862.mp4", 900),
            _FakeBlob("calls/team-sync/20261026T053859Z_guest-alice-b28571b1.mp4", 1000),
            # Older per-track format stays listed.
            _FakeBlob(
                "calls/team-sync/20261026T051340_guest-carol-11112222_microphone_TR_AM1.ogg"
                .replace("051340", "051340Z"),
                50,
            ),
            _FakeBlob("calls/team-sync/EG_abc.json", 400),  # manifest: skipped
            _FakeBlob("calls/team-sync/20261026T060000Z_guest-x-12345678.mp4", 0),  # empty
            _FakeBlob("calls/team-sync-2/20261026T053859Z_guest-eve-1.mp4", 10),  # other call
        ]
    )
    app.state.gcs_client = type("C", (), {"bucket": lambda self, name: bucket})()

    resp = await client.get("/v1/calls/Team-Sync/recordings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["call_id"] == "team-sync"
    recs = body["recordings"]
    assert [r["participant"] for r in recs] == ["Carol", "Alice", "Bob Lee"]
    assert recs[1]["content_type"] == "video/mp4"
    assert recs[1]["source"] == "camera+microphone"
    assert recs[0]["content_type"] == "audio/ogg"
    assert recs[1]["url"].startswith("https://signed.example/calls/team-sync/")


@pytest.mark.asyncio
async def test_end_call_for_everyone(client, call_room):
    import json

    alice = await _join(client, "Alice")
    call_room.publish("guest-alice-1", "TR_A_MIC", video=False)
    auth = {"Authorization": f"Bearer {alice}"}
    await client.post("/v1/calls/team-sync/recording/start", headers=auth)
    assert call_room.recorded() == {"guest-alice-1"}

    resp = await client.post("/v1/calls/team-sync/end", headers=auth)
    assert resp.status_code == 202
    assert resp.json() == {"ended": True, "ended_by": "Alice"}
    # Recording finalized, who-ended published, room deleted (disconnects all).
    assert call_room.recorded() == set()
    assert json.loads(call_room.metadata) == {"recording": False, "ended_by": "Alice"}
    assert call_room.deleted
    info = (await client.get("/v1/calls/team-sync")).json()
    assert info["room_active"] is False and info["participant_count"] == 0

    # Ending an already-ended call is harmless.
    resp = await client.post("/v1/calls/team-sync/end", headers=auth)
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_end_call_requires_this_calls_token(client, call_room):
    resp = await client.post("/v1/calls/team-sync/end")
    assert resp.status_code == 401
    other = (
        await client.post("/v1/calls/other-call/join", json={"display_name": "Eve"})
    ).json()["livekit"]["token"]
    resp = await client.post(
        "/v1/calls/team-sync/end", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 403
    assert not call_room.deleted



@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, location",
    [
        ("/?=team-standup", "/?call=team-standup"),
        ("/?team-standup", "/?call=team-standup"),
        ("/?call=Team%20Standup", "/?call=team-standup"),
        ("/call?call=team-standup", "/?call=team-standup"),
        ("/call?=team-standup", "/?call=team-standup"),
        ("/?=team-standup&name=Ann", "/?call=team-standup&name=Ann"),
    ],
)
async def test_meeting_code_shorthands_redirect_to_named_param(
    client, open_calls, path, location
):
    resp = await client.get(path)
    assert resp.status_code == 302, path
    assert resp.headers["location"] == location


@pytest.mark.asyncio
async def test_query_shorthand_edge_cases(client, open_calls):
    # Other params alone are not a code: new-meeting join screen.
    resp = await client.get("/?name=Ann")
    assert resp.status_code == 200 and "window.APP_CALL_ID = null;" in resp.text
    # Canonical form with extra params is served as-is.
    resp = await client.get("/?call=team-standup&name=Ann")
    assert resp.status_code == 200 and 'window.APP_CALL_ID = "team-standup";' in resp.text
    # A ?session= link is not a meeting code; the client handles it.
    resp = await client.get("/?session=abc")
    assert resp.status_code == 200 and "window.APP_CALL_ID = null;" in resp.text
    # An invalid code is shown on the join screen (with its validation error).
    resp = await client.get("/?call=-bad-")
    assert resp.status_code == 200 and 'window.APP_CALL_ID = "-bad-";' in resp.text


@pytest.mark.asyncio
async def test_page_carries_google_maps_config(client, open_calls, monkeypatch):
    monkeypatch.setattr(calls_module.settings, "google_maps_api_key", "")
    monkeypatch.setattr(calls_module.settings, "google_maps_map_id", "")
    resp = await client.get("/?call=team-sync")
    assert 'window.APP_CONFIG = {"googleMapsKey": null, "googleMapsMapId": null};' in resp.text
    monkeypatch.setattr(calls_module.settings, "google_maps_api_key", "AIza-test")
    monkeypatch.setattr(calls_module.settings, "google_maps_map_id", "abc123")
    resp = await client.get("/?call=team-sync")
    assert '"googleMapsKey": "AIza-test", "googleMapsMapId": "abc123"' in resp.text
