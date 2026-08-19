"""Tests for health/readiness endpoints and their Prometheus gauges."""

from __future__ import annotations

import pytest

from app import metrics


@pytest.mark.asyncio
async def test_healthz_returns_ok(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_reports_checks(client):
    response = await client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "unhealthy")
    assert "checks" in data
    assert "postgres" in data["checks"]


@pytest.mark.asyncio
async def test_readyz_sets_dependency_gauge(client):
    # Force a scrape so the gauge is populated.
    await client.get("/readyz")

    postgres_value = metrics.READYZ_DEPENDENCY_HEALTHY.labels(name="postgres")._value.get()
    assert postgres_value in (0.0, 1.0)
