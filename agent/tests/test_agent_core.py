"""Unit tests for Phase 2 agent worker core."""

from __future__ import annotations

import os
from typing import Any

import pytest
from livekit import agents
from livekit.agents import llm

from agent.main import SupportAgent
from agent.state import AgentMode, SessionState


class _FakeSink:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, **kwargs) -> None:
        self.emitted.append(kwargs)


def _settings() -> Any:
    # Minimal settings stand-in for tests that only need wake phrases.
    class _S:
        wake_phrases = ("hey assistant", "hey agent", "assistant,", "hey ai")

    return _S()


@pytest.fixture(autouse=True)
def _dummy_env():
    """Prevent pydantic-settings from failing during import-time construction."""
    os.environ.setdefault("LIVEKIT_URL", "wss://fake")
    os.environ.setdefault("LIVEKIT_API_KEY", "key")
    os.environ.setdefault("LIVEKIT_API_SECRET", "secret")
    os.environ.setdefault("SERVICE_API_KEY", "svc")


async def _turn(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


@pytest.mark.asyncio
async def test_assisted_mode_silences_without_direct_address(monkeypatch):
    monkeypatch.setattr("agent.main.get_agent_settings", _settings)
    state = SessionState(
        session_id="s1",
        caller_identity="caller-1",
        mode=AgentMode.ASSISTED,
        ai_enabled=True,
    )
    agent = SupportAgent(state, sink=_FakeSink())

    with pytest.raises(agents.StopResponse):
        await agent.on_user_turn_completed(None, await _turn("so then I restarted it"))


@pytest.mark.asyncio
async def test_assisted_mode_replies_when_directly_addressed(monkeypatch):
    monkeypatch.setattr("agent.main.get_agent_settings", _settings)
    state = SessionState(
        session_id="s1",
        caller_identity="caller-1",
        mode=AgentMode.ASSISTED,
        ai_enabled=True,
    )
    sink = _FakeSink()
    agent = SupportAgent(state, sink=sink)

    # Should not raise StopResponse.
    await agent.on_user_turn_completed(None, await _turn("hey assistant, what was the order?"))
    assert len(sink.emitted) == 1
    assert sink.emitted[0]["source"] == "agent_stt"


@pytest.mark.asyncio
async def test_ai_disabled_stops_response(monkeypatch):
    monkeypatch.setattr("agent.main.get_agent_settings", _settings)
    state = SessionState(
        session_id="s1",
        caller_identity="caller-1",
        mode=AgentMode.SOLO,
        ai_enabled=False,
    )
    agent = SupportAgent(state, sink=_FakeSink())

    with pytest.raises(agents.StopResponse):
        await agent.on_user_turn_completed(None, await _turn("hello"))


@pytest.mark.asyncio
async def test_solo_mode_replies_freely(monkeypatch):
    monkeypatch.setattr("agent.main.get_agent_settings", _settings)
    state = SessionState(
        session_id="s1",
        caller_identity="caller-1",
        mode=AgentMode.SOLO,
        ai_enabled=True,
    )
    sink = _FakeSink()
    agent = SupportAgent(state, sink=sink)

    await agent.on_user_turn_completed(None, await _turn("hello"))
    assert len(sink.emitted) == 1
