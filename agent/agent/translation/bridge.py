"""LiveKit glue for the translation bridge.

The bridge joins a room where every human peer is audio-only (no camera is
required or subscribed). For each remote participant it:

  * publishes one local audio track named ``translation-<identity>`` — that
    participant's private translation feed. Clients subscribe ONLY to their
    own feed, which is what enforces "every other user except the speaker":
    the speaker's feed never carries their own utterances.
  * runs an independent STT stream on the participant's microphone
    (separate ``inference.STT`` per stream — sharing one is not safe).
  * on each finalized utterance, hands it to ``TranslationCore`` and, for
    every delivery, enqueues the synthesized audio on the target's feed and
    publishes the transcript record on the chat data topic.

Control plane (topic ``rs.translation.control``):

  * ``{"mode": "NATIVE_RELAY" | "ENGLISH_RELAY"}`` switches the relay mode.
  * ``{"preferred_language": "zh"}`` declares the sender's own listen language,
    overriding detection for messages spoken TO them.
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit import rtc
from livekit.agents import inference, stt

from .core import RelayMode, TranslationCore

logger = logging.getLogger("translation.bridge")

CHAT_TOPIC = "rs.translation.chat"
CONTROL_TOPIC = "rs.translation.control"

TRACK_PREFIX = "translation-"


class _ListenerFeed:
    """One published audio track + queue per remote participant."""

    def __init__(self, room: rtc.Room, identity: str, sample_rate: int) -> None:
        self.identity = identity
        self._source = rtc.AudioSource(sample_rate=sample_rate, num_channels=1)
        self.track = rtc.LocalAudioTrack.create_audio_track(
            name=f"{TRACK_PREFIX}{identity}", source=self._source
        )
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._feeder: asyncio.Task | None = None

    async def publish(self, room: rtc.Room) -> None:
        await room.local_participant.publish_track(
            self.track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        self._feeder = asyncio.create_task(self._feed())

    async def _feed(self) -> None:
        """Pace PCM chunks through the AudioSource in order."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                break
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=self._source.sample_rate,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await self._source.capture_frame(frame)

    def enqueue(self, pcm: bytes) -> None:
        # 10ms frames keep capture_frame's pacing smooth.
        bytes_per_frame = int(self._source.sample_rate * 2 * 0.01)
        for off in range(0, len(pcm), bytes_per_frame):
            self._queue.put_nowait(pcm[off : off + bytes_per_frame])

    async def aclose(self) -> None:
        self._queue.put_nowait(None)
        if self._feeder is not None:
            await asyncio.gather(self._feeder, return_exceptions=True)


class TranslationBridge:
    def __init__(
        self,
        *,
        room: rtc.Room,
        core: TranslationCore,
        stt_model: str,
        synthesizer_sample_rate: int = 24000,
        sink=None,
        detect_language: bool = True,
    ) -> None:
        self._room = room
        self._core = core
        self._stt_model = stt_model
        self._sink = sink
        self._detect_language = detect_language
        self._feeds: dict[str, _ListenerFeed] = {}
        self._stt_tasks: dict[str, asyncio.Task] = {}
        self._last_finals: dict[str, str] = {}
        self._sample_rate = synthesizer_sample_rate

    async def start(self) -> None:
        """Wire room events and set up everyone already present."""
        self._wire()
        for p in self._room.remote_participants.values():
            await self._on_participant_joined(p)
            # track_subscribed only fires for NEW subscriptions — attach to
            # audio tracks that were already publishing when we joined.
            for publication in p.track_publications.values():
                if (
                    publication.kind == rtc.TrackKind.KIND_AUDIO
                    and publication.track is not None
                ):
                    self._on_track_subscribed(
                        publication.track, publication, p
                    )

    def _wire(self) -> None:
        # rtc event emitters reject async callbacks — schedule tasks instead.
        self._room.on("participant_connected")(self._on_participant_joined_sync)
        self._room.on("participant_disconnected")(self._on_participant_left_sync)
        self._room.on("track_subscribed")(self._on_track_subscribed)
        self._room.on("data_received")(self._on_data_received)

    def _on_participant_joined_sync(self, p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(self._on_participant_joined(p))

    def _on_participant_left_sync(self, p: rtc.RemoteParticipant) -> None:
        asyncio.create_task(self._on_participant_left(p))

    async def _on_participant_joined(self, p: rtc.RemoteParticipant) -> None:
        identity = p.identity
        self._core.registry.add(identity)
        feed = _ListenerFeed(self._room, identity, self._sample_rate)
        self._feeds[identity] = feed
        try:
            await feed.publish(self._room)
        except Exception:
            logger.exception("failed to publish translation feed for %s", identity)
        logger.info("translation feed published for %s", identity)

    async def _on_participant_left(self, p: rtc.RemoteParticipant) -> None:
        identity = p.identity
        self._core.registry.remove(identity)
        task = self._stt_tasks.pop(identity, None)
        if task is not None:
            task.cancel()
        feed = self._feeds.pop(identity, None)
        if feed is not None:
            await feed.aclose()

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.TrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return  # audio-only bridge: never subscribe to camera tracks
        if participant.identity == self._room.local_participant.identity:
            return
        task = self._stt_tasks.get(participant.identity)
        if task is not None and not task.done():
            task.cancel()
        task = asyncio.create_task(self._pump_stt(participant.identity, track))
        self._stt_tasks[participant.identity] = task

    async def _pump_stt(self, identity: str, track: rtc.AudioTrack) -> None:
        """Frames in, finalized transcripts out — one independent STT stream."""
        # Provider-side language detection is essential (a multilingual room
        # has no single language to pin) — but the option name is provider-
        # specific, and some models reject it outright (deepgram multi).
        extra = None
        if self._detect_language:
            if self._stt_model.startswith("deepgram/"):
                extra = {"detect_language": True}
            elif self._stt_model.startswith("assemblyai/"):
                extra = {"language_detection": True}
        stt_instance = inference.STT(model=self._stt_model, extra_kwargs=extra)
        stream = stt_instance.stream()
        audio_stream = rtc.AudioStream(track)

        async def _pump() -> None:
            async for ev in audio_stream:
                stream.push_frame(ev.frame)

        async def _drain() -> None:
            async for ev in stream:
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                    alt = ev.alternatives[0]
                    await self._on_final(identity, alt)

        try:
            await asyncio.gather(_pump(), _drain())
        except asyncio.CancelledError:
            pass
        finally:
            await stream.aclose()

    async def _on_final(self, identity: str, alt) -> None:
        if not alt.text or not alt.text.strip():
            logger.debug("dropping empty final transcript from %s", identity)
            return
        # AssemblyAI multilingual can emit near-duplicate finals for one
        # utterance (punctuated + raw). Skip a repeat of the previous final.
        last = self._last_finals.get(identity)
        if last is not None and last == alt.text.strip().lower():
            logger.debug("dropping duplicate final from %s", identity)
            return
        self._last_finals[identity] = alt.text.strip().lower()
        logger.info(
            "final from %s (stt lang=%s): %r", identity, alt.language, alt.text[:80]
        )
        event = await self._core.handle_turn(
            speaker=identity, text=alt.text, detected_language=alt.language
        )
        self._emit_sink(event)
        await self._dispatch(event)

    def _emit_sink(self, event) -> None:
        """Durable transcript: original + one record per relayed delivery."""
        if self._sink is None:
            return
        self._sink.emit(
            role="participant",
            identity=event.speaker,
            source="translation_stt",
            text=event.text,
            language=event.source_language,
        )
        for d in event.deliveries:
            self._sink.emit(
                role="translation",
                identity=d.target_identity,
                source="translation_relay",
                text=d.translated_text,
                language=d.target_language,
            )

    async def _dispatch(self, event) -> None:
        for delivery in event.deliveries:
            feed = self._feeds.get(delivery.target_identity)
            if feed is not None:
                if delivery.audio:
                    feed.enqueue(delivery.audio)
                else:
                    logger.warning(
                        "empty relay audio for %s (%s): TTS produced no frames",
                        delivery.target_identity,
                        delivery.target_language,
                    )
            await self._publish_chat(event, delivery)

    async def _publish_chat(self, event, delivery) -> None:
        payload = json.dumps(
            {
                "id": event.event_id,
                "from": event.speaker,
                "from_language": event.source_language,
                "text": event.text,
                "to": delivery.target_identity,
                "to_language": delivery.target_language,
                "translation": delivery.translated_text,
                "mode": event.mode.value,
            }
        ).encode("utf-8")
        await self._room.local_participant.publish_data(
            payload, topic=CHAT_TOPIC, destination_identities=[delivery.target_identity]
        )

    def _on_data_received(self, packet: rtc.DataPacket) -> None:
        if packet.topic != CONTROL_TOPIC or packet.participant is None:
            return
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("malformed translation control packet")
            return
        identity = packet.participant.identity
        if "mode" in payload:
            try:
                mode = RelayMode(payload["mode"])
            except ValueError:
                logger.warning("unknown relay mode: %r", payload["mode"])
                return
            asyncio.create_task(self._core.set_mode(mode))
        if "preferred_language" in payload:
            self._core.registry.require(identity).set_preferred(payload["preferred_language"])

    async def aclose(self) -> None:
        for task in self._stt_tasks.values():
            task.cancel()
        for feed in self._feeds.values():
            await feed.aclose()
