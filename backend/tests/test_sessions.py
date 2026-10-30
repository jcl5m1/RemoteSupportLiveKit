"""Tests for session lifecycle endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import Session


@pytest.mark.asyncio
async def test_create_session_returns_code_and_consent(db, client):
    response = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1", "display_name": "Sam"},
    )
    assert response.status_code == 201
    data = response.json()
    assert "session_id" in data
    assert "join_code" in data
    assert len(data["join_code"]) == 6
    assert data["consent_required"] is True
    assert "caller_session_token" in data
    assert "livekit" not in data  # consent gate


@pytest.mark.asyncio
async def test_consent_acceptance_returns_livekit_credentials(db, client):
    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1", "display_name": "Sam"},
    )
    create_data = create_resp.json()
    session_id = create_data["session_id"]
    token = create_data["caller_session_token"]

    consent_resp = await client.post(
        f"/v1/sessions/{session_id}/consent",
        json={"accepted": True, "consent_text_version": "v1.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert consent_resp.status_code == 200
    data = consent_resp.json()
    assert data["accepted"] is True
    assert data["recording_enabled"] is True
    assert data["livekit"] is not None
    assert data["livekit"]["identity"].startswith("caller-")


@pytest.mark.asyncio
async def test_consent_decline_without_fallback(db, client):
    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1"},
    )
    create_data = create_resp.json()
    session_id = create_data["session_id"]
    token = create_data["caller_session_token"]

    consent_resp = await client.post(
        f"/v1/sessions/{session_id}/consent",
        json={"accepted": False, "consent_text_version": "v1.0"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert consent_resp.status_code == 200
    data = consent_resp.json()
    assert data["accepted"] is False
    assert data["session_state"] == "consent_declined"
    assert data["livekit"] is None


@pytest.mark.asyncio
async def test_join_requires_auth(db, client):
    response = await client.post("/v1/sessions/join", json={"join_code": "K7R2XM"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_support_join_by_code(db, client, monkeypatch):
    # Bypass Firebase auth for support in this test.
    from app.core.firebase_auth import FirebaseTokenVerifier, SupportPrincipal

    def fake_verify(self, token: str):
        return SupportPrincipal(
            user_id="u_123", email="dana@example.com", display_name="Dana",
            is_admin=False, hosted_domain="example.com",
        )

    monkeypatch.setattr(FirebaseTokenVerifier, "verify", fake_verify)

    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1", "display_name": "Sam"},
    )
    create_data = create_resp.json()
    session_id = create_data["session_id"]
    code = create_data["join_code"]

    join_resp = await client.post(
        "/v1/sessions/join",
        json={"join_code": code, "display_name": "Dana"},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert join_resp.status_code == 200
    data = join_resp.json()
    assert data["session_id"] == session_id
    assert data["livekit"]["identity"].startswith("support-")


def _fake_support_verifier(monkeypatch):
    from app.core.firebase_auth import FirebaseTokenVerifier, SupportPrincipal

    def fake_verify(self, token: str):
        return SupportPrincipal(
            user_id="u_123", email="dana@example.com", display_name="Dana",
            is_admin=False, hosted_domain="example.com",
        )

    monkeypatch.setattr(FirebaseTokenVerifier, "verify", fake_verify)


async def test_support_join_by_session_id(db, client, monkeypatch):
    """Authenticated support can join via /join/<session_id> links, which
    outlive the 6-char join code TTL."""
    _fake_support_verifier(monkeypatch)

    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1", "display_name": "Sam"},
    )
    session_id = create_resp.json()["session_id"]

    # Expire the join code; session-id joins must still work.
    from sqlalchemy import update

    from app.models import Session as SessionModel

    async with db.begin():
        await db.execute(
            update(SessionModel)
            .where(SessionModel.id == session_id)
            .values(join_code_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    join_resp = await client.post(
        "/v1/sessions/join",
        json={"session_id": session_id, "display_name": "Dana"},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert join_resp.status_code == 200
    data = join_resp.json()
    assert data["session_id"] == session_id
    assert data["livekit"]["identity"].startswith("support-")


async def test_support_join_by_session_id_not_found(db, client, monkeypatch):
    _fake_support_verifier(monkeypatch)

    join_resp = await client.post(
        "/v1/sessions/join",
        json={"session_id": str(uuid4())},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert join_resp.status_code == 404
    assert join_resp.json()["error"]["code"] == "code_not_found"


async def test_join_requires_exactly_one_lookup(db, client, monkeypatch):
    _fake_support_verifier(monkeypatch)

    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1", "display_name": "Sam"},
    )
    code = create_resp.json()["join_code"]
    session_id = create_resp.json()["session_id"]

    for payload in ({}, {"join_code": code, "session_id": session_id}):
        resp = await client.post(
            "/v1/sessions/join",
            json=payload,
            headers={"Authorization": "Bearer fake-support-token"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"


async def test_public_config(db, client):
    resp = await client.get("/v1/sessions/public-config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["firebase_project_id"]
    assert data["firebase_auth_domain"].endswith(".firebaseapp.com")
    assert "firebase_api_key" in data


@pytest.mark.asyncio
async def test_support_join_role_occupied(db, client, monkeypatch):
    from app.core.firebase_auth import FirebaseTokenVerifier, SupportPrincipal

    def fake_verify(self, token: str):
        return SupportPrincipal(
            user_id="u_123", email="dana@example.com", display_name="Dana",
            is_admin=False, hosted_domain="example.com",
        )

    monkeypatch.setattr(FirebaseTokenVerifier, "verify", fake_verify)

    create_resp = await client.post(
        "/v1/sessions",
        json={"device_id": "device-1"},
    )
    code = create_resp.json()["join_code"]

    first = await client.post(
        "/v1/sessions/join",
        json={"join_code": code},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/sessions/join",
        json={"join_code": code},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "role_occupied"


@pytest.mark.asyncio
async def test_join_expired_code(db, client, monkeypatch):
    from app.core.firebase_auth import FirebaseTokenVerifier, SupportPrincipal

    def fake_verify(self, token: str):
        return SupportPrincipal(
            user_id="u_123", email="dana@example.com", display_name="Dana",
            is_admin=False, hosted_domain="example.com",
        )

    monkeypatch.setattr(FirebaseTokenVerifier, "verify", fake_verify)

    create_resp = await client.post("/v1/sessions", json={"device_id": "device-1"})
    session_id = create_resp.json()["session_id"]

    # Directly expire the code.
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from tests.conftest import _TestSessionLocal

    async with _TestSessionLocal() as db:
        await db.execute(
            update(Session)
            .where(Session.id == uuid.UUID(session_id))
            .values(join_code_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()

    response = await client.post(
        "/v1/sessions/join",
        json={"join_code": create_resp.json()["join_code"]},
        headers={"Authorization": "Bearer fake-support-token"},
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "code_expired"


@pytest.mark.asyncio
async def test_get_session_requires_auth(db, client):
    create_resp = await client.post("/v1/sessions", json={"device_id": "device-1"})
    session_id = create_resp.json()["session_id"]

    response = await client.get(f"/v1/sessions/{session_id}")
    assert response.status_code == 401

    token = create_resp.json()["caller_session_token"]
    response = await client.get(
        f"/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == session_id


@pytest.mark.asyncio
async def test_metrics_endpoint(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "remote_support_sessions_created_total" in response.text


@pytest.mark.asyncio
async def test_end_session_when_room_already_gone(db, client, app):
    """Ending must succeed even if LiveKit already deleted the room (was a 500)."""
    from livekit.api.twirp_client import TwirpError

    create = (
        await client.post("/v1/sessions", json={"device_id": "device-end", "display_name": "Sam"})
    ).json()
    session_id = create["session_id"]
    auth = {"Authorization": f"Bearer {create['caller_session_token']}"}

    async def room_gone(request):
        raise TwirpError("not_found", "requested room does not exist", status=404)

    app.state.lkapi.room.delete_room = room_gone
    resp = await client.post(f"/v1/sessions/{session_id}/end", headers=auth)
    assert resp.status_code == 202
    assert resp.json() == {"status": "ending"}

    # Other LiveKit errors still surface.
    async def server_down(request):
        raise TwirpError("unavailable", "boom", status=503)

    app.state.lkapi.room.delete_room = server_down
    with pytest.raises(TwirpError):
        await client.post(f"/v1/sessions/{session_id}/end", headers=auth)


@pytest.mark.asyncio
async def test_test_sessions_can_skip_agent_and_recording(db, client, monkeypatch):
    """X-Test-Skip-Metered (service key + test endpoints on) runs a session with
    no agent dispatch and no recording, so regression tests don't spend
    LiveKit Cloud quota. Without the service key the header is ignored."""
    from app.routers import sessions as sessions_module
    from app.services import dispatch

    calls = []
    real = dispatch.dispatch_agent

    async def spy(lkapi, session):
        calls.append(session.room_name)
        return await real(lkapi, session)

    monkeypatch.setattr(dispatch, "dispatch_agent", spy)
    monkeypatch.setattr(sessions_module.settings, "allow_test_endpoints", True)

    async def consent(headers):
        device = f"d-{len(calls)}-{headers['X-Service-Key'][:4]}-{uuid.uuid4().hex[:6]}"
        created = (await client.post("/v1/sessions", json={"device_id": device})).json()
        r = await client.post(
            f"/v1/sessions/{created['session_id']}/consent",
            json={"accepted": True, "consent_text_version": "v1.0"},
            headers={"Authorization": f"Bearer {created['caller_session_token']}", **headers},
        )
        assert r.status_code == 200
        return r.json()

    key = sessions_module.settings.service_api_key
    skipped = await consent({"X-Test-Skip-Metered": "1", "X-Service-Key": key})
    assert skipped["recording_enabled"] is False and calls == []

    ignored = await consent({"X-Test-Skip-Metered": "1", "X-Service-Key": "wrong"})
    assert ignored["recording_enabled"] is True and len(calls) == 1

    monkeypatch.setattr(sessions_module.settings, "allow_test_endpoints", False)
    prod_like = await consent({"X-Test-Skip-Metered": "1", "X-Service-Key": key})
    assert prod_like["recording_enabled"] is True and len(calls) == 2
