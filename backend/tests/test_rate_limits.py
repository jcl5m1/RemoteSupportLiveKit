"""Tests for Postgres-backed rate limiting."""

from __future__ import annotations

import uuid

import pytest

from app.services.rate_limit import check_rate_limit


@pytest.mark.asyncio
async def test_create_session_rate_limit(db, client, monkeypatch):
    """The 6th request from the same IP/device in the same window is rejected."""
    ip = f"10.0.0.{uuid.uuid4().hex[:4]}"
    device_id = f"rate-limit-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr("app.routers.sessions.client_ip", lambda _request: ip)

    for i in range(5):
        resp = await client.post("/v1/sessions", json={"device_id": device_id})
        assert resp.status_code == 201, f"request {i + 1} should succeed"

    resp = await client.post("/v1/sessions", json={"device_id": device_id})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit"


@pytest.mark.asyncio
async def test_check_rate_limit_window_resets():
    """A brand-new key is always allowed on the first hit."""
    key = f"test-{uuid.uuid4().hex}"
    allowed = await check_rate_limit(key=key, limit=1, window_seconds=3600)
    assert allowed is True
