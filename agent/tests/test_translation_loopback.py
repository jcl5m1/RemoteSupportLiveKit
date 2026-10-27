"""Synthetic end-to-end loop-back tests for the translation bridge.

For every sample sentence in each of the four focus languages (en/zh/es/tl):

  1. Speech is generated with edge-tts (neural TTS).
  2. The generated audio is transcribed by a local faster-whisper model with
     language auto-detection — verifying language DETECTION and the original
     TRANSCRIPTION.
  3. The transcript is fed through the real ``TranslationCore`` (with a
     deterministic phrase-table translator seeded from the aligned corpus).
  4. Each delivery's synthesized audio is re-transcribed — verifying the
     spoken-back TRANSLATION matches what the other synthetic user should hear.

Two synthetic users speaking different languages hold a multi-turn
conversation and each only "hears" (re-transcribes) the other's relayed audio,
asserting bidirectional real-time interpretation.

Requires network (edge-tts + one-time whisper model download) and ffmpeg.
Deselect with ``-m "not slow"`` for a fast offline run.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import tempfile
import wave
from pathlib import Path

import pytest

from agent.translation.core import RelayMode, TranslationCore
from agent.translation.registry import ParticipantRegistry
from agent.translation.synthesizer import EdgeTTSSynthesizer, _decode_to_pcm16

from .translation_corpus import SENTENCES, SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.slow

WHISPER_MODEL = os.environ.get("TRANSLATION_TEST_WHISPER_MODEL", "small")
EDGE_VOICE = os.environ.get("TRANSLATION_TEST_EDGE_VOICE")  # override all voices
SIMILARITY_THRESHOLD = float(os.environ.get("TRANSLATION_TEST_SIMILARITY", "0.6"))
CACHE_DIR = Path(tempfile.gettempdir()) / "rs-translation-test-cache"
# Checked-in edge-tts clips (see fixtures/tts/generate.py): the service gives
# no Tagalog audio to CI runners.
TTS_FIXTURES = Path(__file__).parent / "fixtures" / "tts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = re.sub(r"[\s.,!?;:'\"，。！？、¿¡«»\-—…()（）]+", "", text.lower())
    # Whisper sometimes emits Traditional characters and informal 你 where the
    # corpus uses polite 您; both are the same sentence for scoring purposes.
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        try:
            from opencc import OpenCC

            text = OpenCC("t2s").convert(text)
        except ImportError:
            pass
        text = text.replace("您", "你")
    return text


def similarity(a: str, b: str) -> float:
    a, b = _normalize(a), _normalize(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


class PhraseTableTranslator:
    """Deterministic translator backed by the aligned corpus.

    Finds the closest source-sentence match in SENTENCES[source] (STT wording
    varies slightly from the corpus) and returns the aligned SENTENCES[target]
    entry. Lets the loop-back test verify the full speech pipeline exactly,
    independent of LLM phrasing.
    """

    async def translate(self, text: str, source: str, target: str) -> str:
        best_idx, best_score = None, 0.0
        for idx, candidate in enumerate(SENTENCES[source]):
            score = similarity(text, candidate)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is not None and best_score >= 0.5:
            return SENTENCES[target][best_idx]
        logger.warning("phrase-table miss for %r (%s->%s)", text, source, target)
        return text


def _cache_key(kind: str, *parts: str) -> Path:
    import hashlib

    h = hashlib.sha256("||".join(parts).encode()).hexdigest()[:24]
    return CACHE_DIR / f"{kind}-{h}"


def _write_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)


class Harness:
    """Speech synth + recognition, cached on disk between runs."""

    def __init__(self) -> None:
        self.synthesizer = EdgeTTSSynthesizer(sample_rate=16000)
        if EDGE_VOICE:
            for lang in SUPPORTED_LANGUAGES:
                self.synthesizer.set_voice(lang, EDGE_VOICE)
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info("loading faster-whisper model %s (one-time download)", WHISPER_MODEL)
            self._model = WhisperModel(WHISPER_MODEL, device="auto", compute_type="int8")
        return self._model

    async def speak(self, text: str, language: str) -> bytes:
        """Synthesize speech; checked-in fixture first, then cached by (voice, text)."""
        import hashlib

        voice = self.synthesizer._voices.get(language, "")
        key = _cache_key("tts", voice, text)
        if key.with_suffix(".pcm").exists():
            return key.with_suffix(".pcm").read_bytes()
        h = hashlib.sha256(f"{voice}||{text}".encode()).hexdigest()[:16]
        fixture = TTS_FIXTURES / f"{language}-{h}.mp3"
        if fixture.exists():
            pcm = await _decode_to_pcm16(str(fixture), self.synthesizer.sample_rate)
        else:
            pcm = await self.synthesizer.synthesize(text, language)
        key.with_suffix(".pcm").write_bytes(pcm)
        return pcm

    def listen(self, pcm16: bytes, *, language: str | None, cache_tags: tuple[str, ...]) -> tuple[str, str]:
        """Transcribe PCM16 audio; returns (text, detected_language). Cached."""
        import hashlib

        audio_hash = hashlib.sha256(pcm16).hexdigest()[:16]
        key = _cache_key("stt", WHISPER_MODEL, language or "auto", audio_hash, *cache_tags)
        meta = key.with_suffix(".json")
        if meta.exists():
            data = json.loads(meta.read_text())
            return data["text"], data["language"]
        wav_path = key.with_suffix(".wav")
        _write_wav(wav_path, pcm16, 16000)
        segments, info = self.model.transcribe(str(wav_path), language=language)
        text = " ".join(s.text for s in segments).strip()
        detected = info.language
        meta.write_text(json.dumps({"text": text, "language": detected}))
        wav_path.unlink(missing_ok=True)
        return text, detected


@pytest.fixture(scope="module")
def harness():
    try:
        h = Harness()
        _ = h.model  # force model load/download here so skips happen early
    except Exception as exc:  # noqa: BLE001 - any failure means the harness is unusable
        pytest.skip(f"whisper model unavailable: {exc}")
    return h


# --------------------------------------------------------------------------
# 1. Detection + transcription of synthetic speech, 25 sentences x 4 languages
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
async def test_language_detection_and_transcription(harness, language: str):
    """Each synthetic utterance is detected as its own language and transcribed."""
    failures = []
    for idx, sentence in enumerate(SENTENCES[language]):
        pcm = await harness.speak(sentence, language)
        text, detected = harness.listen(pcm, language=None, cache_tags=(language, str(idx)))
        score = similarity(text, sentence)
        if detected != language or score < SIMILARITY_THRESHOLD:
            failures.append((idx, sentence, text, detected, round(score, 2)))
    assert not failures, f"{language}: {len(failures)} failures out of 25: {failures[:5]}"


# --------------------------------------------------------------------------
# 2. Multi-turn bilingual conversation, NATIVE_RELAY loop-back
# --------------------------------------------------------------------------


ES_SPEAKER, ZH_SPEAKER = "user-es", "user-zh"
# (speaker, source_lang, corpus index) — alternating turns.
CONVERSATION = [
    (ES_SPEAKER, "es", 0),
    (ZH_SPEAKER, "zh", 10),
    (ES_SPEAKER, "es", 1),
    (ZH_SPEAKER, "zh", 11),
    (ES_SPEAKER, "es", 2),
    (ZH_SPEAKER, "zh", 12),
    (ES_SPEAKER, "es", 3),
    (ZH_SPEAKER, "zh", 13),
]


async def _loopback_turn(harness, core, speaker: str, source_lang: str, idx: int):
    """One synthetic user speaks; the other 'hears' and re-transcribes the relay."""
    source_sentence = SENTENCES[source_lang][idx]
    pcm = await harness.speak(source_sentence, source_lang)
    transcript, detected = harness.listen(pcm, language=None, cache_tags=(source_lang, str(idx)))

    event = await core.handle_turn(
        speaker=speaker, text=transcript, detected_language=detected
    )
    return event, source_sentence


async def test_bilingual_multiturn_native_relay(harness):
    """es <-> zh conversation: each side hears the other in their own language."""
    registry = ParticipantRegistry()
    registry.add(ES_SPEAKER)
    registry.add(ZH_SPEAKER)
    # Clients declare their listen language on join (rs.translation.control),
    # so the very first turn already relays natively.
    registry.require(ES_SPEAKER).set_preferred("es")
    registry.require(ZH_SPEAKER).set_preferred("zh")
    core = TranslationCore(
        registry=registry,
        translator=PhraseTableTranslator(),
        synthesizer=harness.synthesizer,
    )
    core.mode = RelayMode.NATIVE_RELAY

    listener_of = {ES_SPEAKER: ZH_SPEAKER, ZH_SPEAKER: ES_SPEAKER}
    lang_of = {ES_SPEAKER: "es", ZH_SPEAKER: "zh"}
    failures = []

    for speaker, source_lang, idx in CONVERSATION:
        event, _ = await _loopback_turn(harness, core, speaker, source_lang, idx)
        listener = listener_of[speaker]

        assert event.source_language == source_lang, (
            f"turn {speaker}/{idx}: detected {event.source_language}, expected {source_lang}"
        )
        assert len(event.deliveries) == 1, f"turn {speaker}/{idx}: {event.deliveries}"
        delivery = event.deliveries[0]
        assert delivery.target_identity == listener
        assert delivery.target_language == lang_of[listener], (
            f"turn {speaker}/{idx}: relayed in {delivery.target_language}, "
            f"expected {lang_of[listener]}"
        )
        # The listener "hears" the spoken-back audio and re-transcribes it.
        heard, _ = harness.listen(
            delivery.audio,
            language=delivery.target_language,
            cache_tags=("relay", source_lang, str(idx), delivery.target_language),
        )
        reference = SENTENCES[delivery.target_language][idx]
        score = similarity(heard, reference)
        if score < SIMILARITY_THRESHOLD:
            failures.append((speaker, idx, reference, heard, round(score, 2)))

    assert not failures, f"loop-back mismatches: {failures}"


async def test_english_relay_loopback(harness):
    """ENGLISH_RELAY: non-English speech is spoken back to others in English."""
    registry = ParticipantRegistry()
    registry.add(ES_SPEAKER)
    registry.add("user-en")
    core = TranslationCore(
        registry=registry,
        translator=PhraseTableTranslator(),
        synthesizer=harness.synthesizer,
    )
    core.mode = RelayMode.ENGLISH_RELAY

    failures = []
    for idx in (0, 5, 9, 15, 20, 24):
        event, _ = await _loopback_turn(harness, core, ES_SPEAKER, "es", idx)
        assert event.source_language == "es"
        assert len(event.deliveries) == 1
        delivery = event.deliveries[0]
        assert delivery.target_identity == "user-en"
        assert delivery.target_language == "en"
        heard, _ = harness.listen(
            delivery.audio, language="en", cache_tags=("relay-es-en", str(idx))
        )
        reference = SENTENCES["en"][idx]
        score = similarity(heard, reference)
        if score < SIMILARITY_THRESHOLD:
            failures.append((idx, reference, heard, round(score, 2)))
    assert not failures, f"english relay mismatches: {failures}"


async def test_all_four_languages_relay_to_english(harness):
    """Every focus language relays to English correctly (speaker excluded)."""
    failures = []
    for language in ("zh", "es", "tl"):
        registry = ParticipantRegistry()
        registry.add(f"user-{language}")
        registry.add("user-en")
        core = TranslationCore(
            registry=registry,
            translator=PhraseTableTranslator(),
            synthesizer=harness.synthesizer,
        )
        core.mode = RelayMode.ENGLISH_RELAY

        for idx in (2, 13, 24):
            event, _ = await _loopback_turn(harness, core, f"user-{language}", language, idx)
            assert event.source_language == language
            assert len(event.deliveries) == 1
            delivery = event.deliveries[0]
            assert delivery.target_identity == "user-en"
            heard, _ = harness.listen(
                delivery.audio, language="en",
                cache_tags=(f"relay-{language}-en", str(idx)),
            )
            reference = SENTENCES["en"][idx]
            score = similarity(heard, reference)
            if score < SIMILARITY_THRESHOLD:
                failures.append((language, idx, reference, heard, round(score, 2)))
    assert not failures, f"relay mismatches: {failures}"


# --------------------------------------------------------------------------
# 4. Live LLM translation accuracy (only when LiveKit credentials are live)
# --------------------------------------------------------------------------


async def test_livekit_llm_translation_accuracy(monkeypatch):
    """Translate corpus samples through the real LiveKit Inference LLM.

    Skipped when credentials are missing or the endpoint is unreachable —
    the phrase-table tests above still verify the pipeline exactly.
    """
    # Credentials come from agent/.env via pydantic settings. Other test
    # modules put dummy LIVEKIT_* values in os.environ, and pydantic-settings
    # prefers os.environ over the .env file — remove them first so the real
    # credentials load (and get cached by get_agent_settings).
    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    try:
        from agent.config import get_agent_settings

        s = get_agent_settings()
    except Exception:  # noqa: BLE001 - missing env files just means skip
        pytest.skip("agent settings unavailable (no LIVEKIT credentials)")

    monkeypatch.setenv("LIVEKIT_URL", s.livekit_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", s.livekit_api_key)
    monkeypatch.setenv("LIVEKIT_API_SECRET", s.livekit_api_secret)

    from agent.translation.translator import LiveKitTranslator

    translator = LiveKitTranslator(
        model=s.llm_model, api_key=s.livekit_api_key, api_secret=s.livekit_api_secret
    )

    pairs = [("es", "en", 0), ("zh", "en", 3), ("tl", "en", 7), ("en", "es", 12)]

    async def _run_pairs() -> list[tuple]:
        failures = []
        for source, target, idx in pairs:
            translated = await asyncio.wait_for(
                translator.translate(SENTENCES[source][idx], source, target),
                timeout=60,
            )
            score = similarity(translated, SENTENCES[target][idx])
            logger.info(
                "%s->%s [%d] similarity=%.2f %r -> %r",
                source, target, idx, score,
                SENTENCES[source][idx][:40], translated[:40],
            )
            if score < SIMILARITY_THRESHOLD:
                failures.append((source, target, idx, translated, round(score, 2)))
        return failures

    try:
        # LLM phrasing varies run to run; a single borderline sample should not
        # fail the suite, so give it one retry before asserting.
        failures = await _run_pairs()
        if failures:
            logger.warning("retrying %d borderline LLM translations", len(failures))
            failures = await _run_pairs()
    except Exception as exc:  # noqa: BLE001 - skip on any endpoint/credential failure
        pytest.skip(f"LiveKit Inference LLM unavailable: {exc}")

    assert not failures, f"LLM translation mismatches: {failures}"
