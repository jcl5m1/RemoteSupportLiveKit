"""Tests for transcript ingest metrics."""

from __future__ import annotations

import uuid

import pytest

from app import metrics
from app.config import get_settings


@pytest.mark.asyncio
async def test_ingest_utterances_increments_success_metrics(db, client):
    settings = get_settings()
    create_resp = await client.post("/v1/sessions", json={"device_id": "device-tx"})
    session_id = create_resp.json()["session_id"]

    success_before = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "success"})
    utterances_before = _counter_value(
        metrics.UTTERANCES_INGESTED, {"role": "caller", "source": "agent_stt"}
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/utterances",
        json={
            "utterances": [
                {
                    "client_utterance_id": "u1",
                    "role": "caller",
                    "identity": "caller-1",
                    "source": "agent_stt",
                    "text": "hello",
                    "start_ms": 0,
                    "end_ms": 500,
                    "language": "en",
                }
            ]
        },
        headers={"X-Service-Key": settings.service_api_key},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 1
    assert data["duplicates"] == 0

    success_after = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "success"})
    utterances_after = _counter_value(
        metrics.UTTERANCES_INGESTED, {"role": "caller", "source": "agent_stt"}
    )
    assert success_after == success_before + 1
    assert utterances_after == utterances_before + 1


@pytest.mark.asyncio
async def test_ingest_utterances_missing_session_increments_not_found(db, client):
    settings = get_settings()
    missing_id = uuid.uuid4()
    before = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "not_found"})

    resp = await client.post(
        f"/v1/sessions/{missing_id}/utterances",
        json={"utterances": []},
        headers={"X-Service-Key": settings.service_api_key},
    )
    assert resp.status_code == 404

    after = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "not_found"})
    assert after == before + 1


@pytest.mark.asyncio
async def test_ingest_utterances_rate_limit_increments_rate_limited(db, client, monkeypatch):
    settings = get_settings()
    # Use a tiny limit so the test is fast and does not depend on window boundaries.
    monkeypatch.setattr(settings, "utterance_rate_limit", 2)
    monkeypatch.setattr(settings, "utterance_rate_limit_window_seconds", 60)

    create_resp = await client.post("/v1/sessions", json={"device_id": "device-tx-rl"})
    session_id = create_resp.json()["session_id"]
    before = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "rate_limited"})

    # The first two requests are allowed; the third exceeds the limit.
    for _ in range(3):
        await client.post(
            f"/v1/sessions/{session_id}/utterances",
            json={"utterances": []},
            headers={"X-Service-Key": settings.service_api_key},
        )

    resp = await client.post(
        f"/v1/sessions/{session_id}/utterances",
        json={"utterances": []},
        headers={"X-Service-Key": settings.service_api_key},
    )
    assert resp.status_code == 429

    after = _counter_value(metrics.TRANSCRIPT_BATCHES, {"status": "rate_limited"})
    # The 3rd loop request and the explicit follow-up both hit the rate limit.
    assert after == before + 2


def _counter_value(counter, labels):
    return counter.labels(**labels)._value.get()
