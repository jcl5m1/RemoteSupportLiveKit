"""STT-only pipeline for the SUPPORT participant (FR-3.3, FR-3.4).

This class deliberately holds no reference to ``AgentSession.generate_reply``.
Support speech reaches the transcript and the agent's context, and stops there.
If you ever find yourself adding a reply path here, you are breaking the core
requirement -- add it to the caller pipeline instead.
"""

from __future__ import annotations

import asyncio

from livekit import rtc
from livekit.agents import inference, llm, stt


class SupportTranscriber:
    def __init__(self, *, session, agent, state, sink, settings) -> None:
        self._session = session
        self._agent = agent
        self._state = state
        self._sink = sink
        # A separate STT instance -- sharing one across two streams is not safe.
        self._stt = inference.STT(model=settings.effective_support_stt_model)
        self._task: asyncio.Task | None = None

    async def watch(self, room: rtc.Room) -> None:
        """Attach when the support participant publishes audio."""

        @room.on("track_subscribed")
        def _on_track_subscribed(
            track: rtc.Track,
            publication: rtc.TrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if not participant.identity.startswith("support-"):
                return
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            self._task = asyncio.create_task(self.attach(track, participant.identity))

        @room.on("track_unsubscribed")
        def _on_track_unsubscribed(
            publication: rtc.TrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if not participant.identity.startswith("support-"):
                return
            if self._task is not None and not self._task.done():
                self._task.cancel()

    async def attach(self, track: rtc.AudioTrack, identity: str) -> None:
        """Pump frames into STT, drain finals. Interims are discarded (FR-6.3)."""
        stream = self._stt.stream()
        audio_stream = rtc.AudioStream(track)

        async def _pump() -> None:
            async for ev in audio_stream:
                stream.push_frame(ev.frame)

        async def _drain() -> None:
            async for ev in stream:
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    alt = ev.alternatives[0]
                    await self._on_final(identity, alt)

        try:
            await asyncio.gather(_pump(), _drain())
        finally:
            await stream.aclose()

    async def _on_final(self, identity: str, alt: stt.SpeechData) -> None:
        """Persist, then inject as read-only context."""
        self._sink.emit(
            role="support",
            identity=identity,
            source="support_stt",
            text=alt.text,
            language=alt.language,
            confidence=alt.confidence,
        )

        chat_ctx = self._session.history.copy()
        chat_ctx.add_message(llm.ChatMessage(role="user", content=[f"[SUPPORT] {alt.text}"]))
        await self._agent.update_chat_ctx(chat_ctx)
