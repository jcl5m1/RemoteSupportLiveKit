"""Unit tests for the translation core — no audio, no network, no LiveKit."""

from __future__ import annotations

from agent.translation.core import (
    PassthroughTranslator,
    RelayMode,
    TranslationCore,
)
from agent.translation.languages import normalize_language
from agent.translation.registry import ParticipantRegistry

from .translation_corpus import SENTENCES, SUPPORTED_LANGUAGES


class _RecordingSynthesizer:
    sample_rate = 16000

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, language: str) -> bytes:
        self.calls.append((text, language))
        return f"pcm:{language}:{text}".encode()


def _core(mode: RelayMode = RelayMode.ENGLISH_RELAY):
    registry = ParticipantRegistry()
    synth = _RecordingSynthesizer()
    core = TranslationCore(
        registry=registry, translator=PassthroughTranslator(), synthesizer=synth
    )
    core.mode = mode
    return core, registry, synth


def test_corpus_alignment():
    for lang in SUPPORTED_LANGUAGES:
        assert len(SENTENCES[lang]) == 25
        for s in SENTENCES[lang]:
            assert s == s.strip()
            assert not s.endswith((".", "?", "!", "。", "！", "？"))


def test_normalize_language_aliases():
    assert normalize_language("en-US") == "en"
    assert normalize_language("cmn-hans") == "zh"
    assert normalize_language("es_MX") == "es"
    assert normalize_language("fil") == "tl"
    assert normalize_language("eng") == "en"
    assert normalize_language(None) is None
    assert normalize_language("fr") == "fr"  # unsupported codes pass through


async def test_english_relay_translates_non_english_to_all_others():
    core, registry, synth = _core(RelayMode.ENGLISH_RELAY)
    registry.add("user-es")
    registry.add("user-zh")
    registry.add("user-tl")

    event = await core.handle_turn(speaker="user-es", text="hola", detected_language="es")

    assert event.source_language == "es"
    assert {d.target_identity for d in event.deliveries} == {"user-zh", "user-tl"}
    assert all(d.target_language == "en" for d in event.deliveries)
    # Speaker never receives their own utterance.
    assert all(d.target_identity != "user-es" for d in event.deliveries)
    assert ("hola", "en") in synth.calls


async def test_english_relay_skips_english_speech():
    core, registry, synth = _core(RelayMode.ENGLISH_RELAY)
    registry.add("user-en")
    registry.add("user-zh")

    event = await core.handle_turn(speaker="user-en", text="hello there", detected_language="en")

    # Everyone already understood it — nothing to relay.
    assert event.deliveries == []
    assert synth.calls == []


async def test_native_relay_speaks_each_listener_language():
    core, registry, _ = _core(RelayMode.NATIVE_RELAY)
    registry.add("user-es")
    # Listeners declare their language on join (control topic), before speaking.
    registry.require("user-zh").set_preferred("zh")

    # es speaker is heard by a zh listener in Chinese.
    event = await core.handle_turn(speaker="user-es", text="hola", detected_language="es")
    assert len(event.deliveries) == 1
    assert event.deliveries[0].target_identity == "user-zh"
    assert event.deliveries[0].target_language == "zh"

    # Now the zh listener speaks — the es participant hears Spanish.
    event = await core.handle_turn(speaker="user-zh", text="你好", detected_language="zh")
    assert len(event.deliveries) == 1
    assert event.deliveries[0].target_identity == "user-es"
    assert event.deliveries[0].target_language == "es"


async def test_native_relay_skips_same_language_listener():
    core, registry, _ = _core(RelayMode.NATIVE_RELAY)
    registry.add("user-es-1")
    registry.require("user-es-2").set_preferred("es")
    registry.require("user-zh").set_preferred("zh")

    event = await core.handle_turn(speaker="user-es-1", text="hola", detected_language="es")

    # The other Spanish speaker already understood; only the zh listener gets audio.
    assert {d.target_identity for d in event.deliveries} == {"user-zh"}


async def test_native_relay_unknown_listener_falls_back_to_english():
    core, registry, _ = _core(RelayMode.NATIVE_RELAY)
    registry.add("user-es")
    registry.add("user-mystery")

    event = await core.handle_turn(speaker="user-es", text="hola", detected_language="es")

    assert event.deliveries[0].target_identity == "user-mystery"
    assert event.deliveries[0].target_language == "en"


async def test_preferred_language_overrides_detection():
    core, registry, _ = _core(RelayMode.NATIVE_RELAY)
    registry.add("user-zh")
    # A zh-detected user who prefers to hear Spanish.
    listener = registry.require("user-es")
    listener.set_preferred("es")
    listener.record_detection("zh")

    event = await core.handle_turn(speaker="user-zh", text="你好", detected_language="zh")
    assert event.deliveries[0].target_identity == "user-es"
    assert event.deliveries[0].target_language == "es"


async def test_detected_language_is_tracked_per_user():
    core, registry, _ = _core()
    registry.add("user-a")
    registry.add("user-b")

    await core.handle_turn(speaker="user-a", text="hola", detected_language="es")
    await core.handle_turn(speaker="user-a", text="hola otra vez", detected_language="es")

    p = registry.get("user-a")
    assert p.detected_language == "es"
    assert p.detections == {"es": 2}
    assert registry.get("user-b").detected_language is None


async def test_mode_switch_mid_conversation():
    core, registry, _ = _core(RelayMode.ENGLISH_RELAY)
    registry.add("user-es")
    registry.add("user-zh")

    e1 = await core.handle_turn(speaker="user-es", text="hola", detected_language="es")
    assert e1.deliveries[0].target_language == "en"

    await core.set_mode(RelayMode.NATIVE_RELAY)

    # Listener language not yet known -> English fallback.
    e2 = await core.handle_turn(speaker="user-es", text="adiós", detected_language="es")
    assert e2.deliveries[0].target_language == "en"

    # Once the listener's language is known (or declared), they hear it.
    registry.require("user-zh").set_preferred("zh")
    e3 = await core.handle_turn(speaker="user-es", text="gracias", detected_language="es")
    assert e3.deliveries[0].target_language == "zh"


async def test_translation_cached_per_target_language():
    class _CountingTranslator(PassthroughTranslator):
        def __init__(self) -> None:
            self.calls = 0

        async def translate(self, text: str, source: str, target: str) -> str:
            self.calls += 1
            return f"{text}[{target}]"

    registry = ParticipantRegistry()
    registry.add("user-es")
    registry.add("user-zh")
    registry.add("user-tl")
    translator = _CountingTranslator()
    core = TranslationCore(
        registry=registry, translator=translator, synthesizer=_RecordingSynthesizer()
    )

    await core.handle_turn(speaker="user-es", text="hola", detected_language="es")
    # Two listeners, same target language -> one translation call, two deliveries.
    assert translator.calls == 1
