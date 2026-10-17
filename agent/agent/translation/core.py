"""Pure translation decision logic — no LiveKit objects in, no LiveKit objects out.

``TranslationCore.handle_turn()`` takes a finalized utterance from one
participant and produces a ``TranslationEvent`` describing exactly who should
hear what, in which language, with audio already synthesized. The bridge feeds
it; tests drive it directly with synthetic speech.

Relay modes:

  ENGLISH_RELAY  every non-English utterance is translated to English and
                 spoken to all other participants. English utterances are
                 already understood by everyone and are not relayed.
  NATIVE_RELAY   every utterance is translated to each listener's own
                 language (detected, or preferred if declared) and spoken to
                 that listener. Listeners sharing the speaker's language are
                 skipped — they heard the original.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import uuid

from .languages import SUPPORTED_LANGUAGES, normalize_language
from .registry import ParticipantRegistry

logger = logging.getLogger("translation.core")


class RelayMode(str, enum.Enum):
    ENGLISH_RELAY = "ENGLISH_RELAY"
    NATIVE_RELAY = "NATIVE_RELAY"


#: Language spoken when a NATIVE_RELAY listener's language is not yet known.
DEFAULT_FALLBACK_LANGUAGE = "en"


class Translator:
    """Protocol-ish base: translate ``text`` from ``source`` to ``target``."""

    async def translate(self, text: str, source: str, target: str) -> str:
        raise NotImplementedError


class Synthesizer:
    """Protocol-ish base: render ``text`` in ``language`` as PCM16 mono bytes."""

    sample_rate = 16000

    async def synthesize(self, text: str, language: str) -> bytes:
        raise NotImplementedError


class NullSynthesizer(Synthesizer):
    async def synthesize(self, text: str, language: str) -> bytes:
        return b""


class PassthroughTranslator(Translator):
    """Test helper: no-op translation."""

    async def translate(self, text: str, source: str, target: str) -> str:
        return text


class Delivery:
    def __init__(
        self,
        *,
        target_identity: str,
        target_language: str,
        translated_text: str,
        audio: bytes,
    ) -> None:
        self.target_identity = target_identity
        self.target_language = target_language
        self.translated_text = translated_text
        self.audio = audio

    def __repr__(self) -> str:
        return (
            f"Delivery(target={self.target_identity!r}, lang={self.target_language}, "
            f"text={self.translated_text[:40]!r}, audio={len(self.audio)}B)"
        )


class TranslationEvent:
    def __init__(
        self,
        *,
        event_id: str,
        speaker: str,
        source_language: str,
        text: str,
        mode: RelayMode,
        deliveries: list[Delivery],
    ) -> None:
        self.event_id = event_id
        self.speaker = speaker
        self.source_language = source_language
        self.text = text
        self.mode = mode
        self.deliveries = deliveries

    def __repr__(self) -> str:
        return (
            f"TranslationEvent(speaker={self.speaker!r}, lang={self.source_language}, "
            f"mode={self.mode.value}, deliveries={len(self.deliveries)})"
        )


class TranslationCore:
    """Thread the per-utterance pipeline: detect -> decide -> translate -> speak."""

    def __init__(
        self,
        *,
        registry: ParticipantRegistry,
        translator: Translator,
        synthesizer: Synthesizer | None = None,
        fallback_language: str = DEFAULT_FALLBACK_LANGUAGE,
    ) -> None:
        self.registry = registry
        self.translator = translator
        self.synthesizer = synthesizer or NullSynthesizer()
        self.fallback_language = fallback_language
        self.mode = RelayMode.ENGLISH_RELAY
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def set_mode(self, mode: RelayMode) -> None:
        async with self._global_lock:
            self.mode = mode
        logger.info("relay mode set to %s", mode.value)

    def _lock_for(self, speaker: str) -> asyncio.Lock:
        lock = self._locks.get(speaker)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[speaker] = lock
        return lock

    def _target_language(self, listener, source_language: str) -> str | None:
        if self.mode is RelayMode.ENGLISH_RELAY:
            return "en"
        # NATIVE_RELAY: speak to the listener in their own language.
        return listener.listen_language or self.fallback_language

    async def handle_turn(
        self,
        *,
        speaker: str,
        text: str,
        detected_language: str | None,
    ) -> TranslationEvent:
        """Process one finalized utterance; returns the routing decision."""
        async with self._lock_for(speaker):
            event = await self._handle_turn_locked(
                speaker=speaker, text=text, detected_language=detected_language
            )
        logger.info(
            "turn: %s (%s) %r -> %d deliver%s",
            speaker,
            event.source_language,
            event.text[:60],
            len(event.deliveries),
            "y" if len(event.deliveries) == 1 else "ies",
        )
        return event

    async def _handle_turn_locked(
        self, *, speaker: str, text: str, detected_language: str | None
    ) -> TranslationEvent:
        speaker_participant = self.registry.require(speaker)
        source = normalize_language(detected_language) or speaker_participant.detected_language
        # Multilingual STT models report "multi" (or nothing) instead of a
        # language code — ask the translator for a constrained detection.
        if not source or source not in SUPPORTED_LANGUAGES:
            detector = getattr(self.translator, "detect_language", None)
            if detector is not None and text.strip():
                detected = await detector(text)
                if detected:
                    source = detected
        if source:
            speaker_participant.record_detection(source)
        else:
            source = speaker_participant.detected_language or self.fallback_language

        deliveries: list[Delivery] = []
        # Translate once per distinct target language, synthesize once per
        # distinct (language, text) — then fan out to listeners.
        translations: dict[str, str] = {}
        audio_cache: dict[tuple[str, str], bytes] = {}
        for listener in self.registry.others(speaker):
            target_language = self._target_language(listener, source)
            if not target_language:
                continue
            if target_language == source:
                continue  # listener already understood the original
            if target_language not in translations:
                translations[target_language] = await self.translator.translate(
                    text, source, target_language
                )
            audio_key = (target_language, translations[target_language])
            if audio_key not in audio_cache:
                audio_cache[audio_key] = await self.synthesizer.synthesize(
                    translations[target_language], target_language
                )
            deliveries.append(
                Delivery(
                    target_identity=listener.identity,
                    target_language=target_language,
                    translated_text=translations[target_language],
                    audio=audio_cache[audio_key],
                )
            )

        return TranslationEvent(
            event_id=str(uuid.uuid4()),
            speaker=speaker,
            source_language=source,
            text=text,
            mode=self.mode,
            deliveries=deliveries,
        )
