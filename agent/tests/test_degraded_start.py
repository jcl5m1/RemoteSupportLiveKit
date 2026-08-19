"""Degraded-start mode lets the agent worker boot without LiveKit credentials."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from agent.config import AgentSettings
from agent.main import _run_worker


def test_run_worker_degraded_mode_when_missing_credentials(monkeypatch):
    settings = AgentSettings(
        livekit_url="wss://invalid",
        livekit_api_key="",
        livekit_api_secret="",
        service_api_key="test",
        allow_degraded_start=True,
    )
    monkeypatch.setattr("agent.main.get_agent_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["agent.main", "start"])

    run_forever = MagicMock()
    monkeypatch.setattr("agent.main.asyncio.get_event_loop", lambda: MagicMock(run_forever=run_forever))

    cli_run_app = MagicMock()
    monkeypatch.setattr("agent.main.agents.cli.run_app", cli_run_app)

    _run_worker()

    run_forever.assert_called_once()
    cli_run_app.assert_not_called()


def test_run_worker_normal_mode_with_credentials(monkeypatch):
    settings = AgentSettings(
        livekit_url="wss://invalid",
        livekit_api_key="key",
        livekit_api_secret="secret",
        service_api_key="test",
        allow_degraded_start=True,
    )
    monkeypatch.setattr("agent.main.get_agent_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["agent.main", "start"])

    run_forever = MagicMock()
    monkeypatch.setattr("agent.main.asyncio.get_event_loop", lambda: MagicMock(run_forever=run_forever))

    cli_run_app = MagicMock()
    monkeypatch.setattr("agent.main.agents.cli.run_app", cli_run_app)

    _run_worker()

    run_forever.assert_not_called()
    cli_run_app.assert_called_once()
