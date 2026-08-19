"""Degraded-start mode lets the backend boot without cloud credentials."""

from __future__ import annotations

import pytest
from livekit import api

from app.config import Settings
from app.degraded_clients import DegradedGCSClient, DegradedLiveKitAPI
from app.main import lifespan


@pytest.mark.asyncio
async def test_degraded_livekit_api_raises():
    lkapi = DegradedLiveKitAPI()
    with pytest.raises(RuntimeError, match="LiveKit API is degraded"):
        await lkapi.room.list_rooms(api.ListRoomsRequest())


def test_degraded_gcs_client_raises():
    client = DegradedGCSClient("test-bucket")
    with pytest.raises(RuntimeError, match="GCS client is degraded"):
        client.bucket("test-bucket").exists()


@pytest.mark.asyncio
async def test_lifespan_uses_degraded_clients_when_allowed(monkeypatch):
    settings = Settings(
        livekit_url="wss://invalid",
        livekit_api_key="",
        livekit_api_secret="",
        database_url="postgresql+asyncpg://rs:rs@localhost:5432/remote_support_test",
        gcs_bucket="test-bucket",
        gcp_credentials_b64="",
        caller_jwt_secret="test",
        service_api_key="test",
        firebase_project_id="test",
        allow_degraded_start=True,
    )

    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _level: None)

    class _BrokenLiveKitAPI:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no credentials")

    class _BrokenGCSClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("no credentials")

    monkeypatch.setattr(main_module.api, "LiveKitAPI", _BrokenLiveKitAPI)
    monkeypatch.setattr(main_module.gcs, "Client", _BrokenGCSClient)

    app = main_module.FastAPI()
    async with lifespan(app):
        assert isinstance(app.state.lkapi, DegradedLiveKitAPI)
        assert isinstance(app.state.gcs_client, DegradedGCSClient)
