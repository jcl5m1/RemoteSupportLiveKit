"""Tests for session lifecycle endpoints."""

from __future__ import annotations

import uuid

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
