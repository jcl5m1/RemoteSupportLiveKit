"""AI enable/disable control plane, agent side (FR-4.x).

Two inbound paths, both idempotent:

  * room metadata  -- authoritative, durable, survives a worker restart
  * data message   -- fast path on topic "rs.agent.control"

Plus a 30s heartbeat that reconciles against the backend, which is what makes a
lost metadata update self-healing.
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit import rtc

from .prompts import compose_instructions
from .state import AgentMode

logger = logging.getLogger("support-agent.control")

CONTROL_TOPIC = "rs.agent.control"
HEARTBEAT_SECONDS = 30


class ControlPlane:
    def __init__(self, *, session, agent, state, settings) -> None:
        self._session = session
        self._agent = agent
        self._state = state
        self._settings = settings
        self._heartbeat_task: asyncio.Task | None = None

    def attach(self, room: rtc.Room) -> None:
        """Register room-metadata and data-message listeners."""

        @room.on("room_metadata_changed")
        def _on_metadata_changed(metadata: str):
            try:
                m = json.loads(metadata)
            except json.JSONDecodeError:
                logger.warning("received non-JSON room metadata: %r", metadata)
                return
            v = m.get("v", 0)
            if v <= self._state.metadata_version:
                return  # stale; drop (docs/02)
            self._state.metadata_version = v
            asyncio.create_task(self.apply(m.get("ai_enabled", self._state.ai_enabled)))
            asyncio.create_task(self._reconcile_mode(m.get("agent_mode", self._state.mode.value)))

        @room.on("data_received")
        def _on_data_received(packet: rtc.DataPacket):
            if packet.topic != CONTROL_TOPIC:
                return
            participant = getattr(packet, "participant", None)
            if participant is None or not participant.identity.startswith("support-"):
                # The fast path bypasses backend auth; re-authorize locally.
                return
            try:
                payload = json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("received malformed control packet")
                return
            enabled = payload.get("enabled")
            if not isinstance(enabled, bool):
                return
            asyncio.create_task(self.apply(enabled))

    async def apply(self, enabled: bool) -> None:
        """Apply the toggle. 'Mute speech, keep transcribing.'"""
        if self._state.ai_enabled == enabled:
            return
        if not enabled:
            self._session.interrupt()  # cut off mid-word
        self._session.output.set_audio_enabled(enabled)  # stop speaking
        self._session.output.set_transcription_enabled(True)  # captions stay
        self._session.input.set_audio_enabled(True)  # keep listening
        self._state.ai_enabled = enabled
        logger.info("ai_enabled toggled to %s", enabled)

    async def start_heartbeat(self) -> None:
        """Every 30s, GET /v1/sessions/{id} and reconcile state."""

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._settings.heartbeat_seconds)
                try:
                    await self._reconcile()
                except Exception:
                    logger.exception("heartbeat reconciliation failed")

        self._heartbeat_task = asyncio.create_task(_loop())

    async def _reconcile(self) -> None:
        if self._state._http is None:
            return
        r = await self._state._http.get(
            f"/v1/sessions/{self._state.session_id}",
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()

        remote_version = data.get("metadata_version", self._state.metadata_version)
        if remote_version > self._state.metadata_version:
            self._state.metadata_version = remote_version
            await self.apply(data.get("ai_enabled", self._state.ai_enabled))
            await self._reconcile_mode(data.get("agent_mode", self._state.mode.value))

    async def _reconcile_mode(self, mode_value: str) -> None:
        try:
            new_mode = AgentMode(mode_value)
        except ValueError:
            logger.warning("unknown agent_mode from backend: %s", mode_value)
            return
        if new_mode == self._state.mode:
            return
        self._state.mode = new_mode
        await self._agent.update_instructions(compose_instructions(new_mode))
        await self._state.report_mode()
