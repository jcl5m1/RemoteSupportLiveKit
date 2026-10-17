"""TTS implementations for the translation bridge.

All implementations conform to the ``Synthesizer`` protocol from core.py:
``synthesize(text, language) -> PCM16 mono bytes``. The bridge feeds those
bytes to per-listener ``rtc.AudioSource`` tracks.

  LiveKitTTSSynthesizer  production path; per-language voice map of
                         ``provider/model:voice`` identifiers resolved through
                         LiveKit Cloud Inference.
  EdgeTTSSynthesizer     local/network fallback (used by the synthetic test
                         harness); Microsoft Edge neural voices, decoded to
                         PCM via ffmpeg.
  SaySynthesizer         offline macOS `say` fallback (en/zh/es only; macOS
                         ships no Tagalog voice).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile

from livekit.agents import inference

logger = logging.getLogger("translation.synthesizer")

#: edge-tts voice per language.
EDGE_VOICES = {
    "en": "en-US-JennyNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "es": "es-ES-ElviraNeural",
    "tl": "fil-PH-BlessicaNeural",
}

#: macOS `say` voice per language.
SAY_VOICES = {
    "en": "Samantha",
    "zh": "Ting-Ting",
    "es": "Monica",
}


class LiveKitTTSSynthesizer:
    """Per-language voices through LiveKit Cloud Inference.

    Voice map values are full ``provider/model:voice_id`` identifiers. A
    missing language falls back to the default voice identifier.
    """

    def __init__(self, voices: dict[str, str], default_voice: str, sample_rate: int = 24000) -> None:
        self._voices = voices
        self._default = default_voice
        self._sample_rate = sample_rate
        self._cache: dict[str, inference.TTS] = {}

    def _tts_for(self, language: str) -> inference.TTS:
        model = self._voices.get(language, self._default)
        t = self._cache.get(model)
        if t is None:
            t = inference.TTS(model=model)
            self._cache[model] = t
        return t

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str, language: str) -> bytes:
        t = self._tts_for(language)
        stream = t.synthesize(text)
        pcm = bytearray()
        async for frame in stream:
            pcm.extend(frame.frame.data)
        return bytes(pcm)


async def _decode_to_pcm16(audio_path: str, sample_rate: int) -> bytes:
    """Decode any ffmpeg-readable file to raw PCM16 mono at ``sample_rate``."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", audio_path,
        "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "pipe:1",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {err.decode()[:200]}")
    return out


class EdgeTTSSynthesizer:
    """Neural TTS via edge-tts (network required). Used by the test harness."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self._sample_rate = sample_rate
        self._voices = dict(EDGE_VOICES)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def set_voice(self, language: str, voice: str) -> None:
        self._voices[language] = voice

    async def synthesize(self, text: str, language: str) -> bytes:
        import edge_tts

        voice = self._voices.get(language)
        if voice is None:
            raise ValueError(f"no edge-tts voice configured for {language!r}")
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        try:
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    await edge_tts.Communicate(text, voice).save(path)
                    break
                except Exception as exc:  # noqa: BLE001 - network flakes are transient
                    last_exc = exc
                    logger.warning("edge-tts attempt %d failed: %s", attempt + 1, exc)
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                raise RuntimeError(f"edge-tts failed after 3 attempts: {last_exc}")
            return await _decode_to_pcm16(path, self._sample_rate)
        finally:
            import os

            os.unlink(path)


class SaySynthesizer:
    """Offline macOS `say` TTS. No Tagalog voice ships with macOS."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self._sample_rate = sample_rate

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def synthesize(self, text: str, language: str) -> bytes:
        voice = SAY_VOICES.get(language)
        if voice is None:
            raise ValueError(f"no macOS say voice for {language!r}")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", voice, "-o", path, "--data-format=LEI16@16000", text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"say failed: {err.decode()[:200]}")
            return await _decode_to_pcm16(path, self._sample_rate)
        finally:
            import os

            os.unlink(path)
