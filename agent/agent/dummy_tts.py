"""Placeholder TTS for headless regression.

LiveKit Cloud Inference TTS fails in this project with ``no audio frames were
pushed`` for every model/voice.  Rather than leave the agent speech scheduler
stuck, this minimal local TTS emits a simple sine-wave tone for the estimated
duration of each reply.  The audio is not intelligible speech, but it satisfies
the agent runtime so text replies continue to flow to the transcript sink.
"""

from __future__ import annotations

import math
import struct

from livekit.agents import tts
from livekit.agents.types import APIConnectOptions


class _ToneChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: ToneTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tone_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id="tone-tts-chunked",
            sample_rate=self._tone_tts.sample_rate,
            num_channels=self._tone_tts.num_channels,
            mime_type="audio/pcm",
            frame_size_ms=200,
            stream=False,
        )
        pcm = self._tone_tts.generate_pcm(self._input_text)
        if pcm:
            output_emitter.push(pcm)
        output_emitter.end_input()


class _ToneSynthesizeStream(tts.SynthesizeStream):
    def __init__(
        self,
        *,
        tts: ToneTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._tone_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id="tone-tts-stream",
            sample_rate=self._tone_tts.sample_rate,
            num_channels=self._tone_tts.num_channels,
            mime_type="audio/pcm",
            frame_size_ms=200,
            stream=True,
        )

        buffered = ""
        async for event in self._input_ch:
            if isinstance(event, tts.SynthesizeStream._FlushSentinel):
                if buffered.strip():
                    output_emitter.start_segment(segment_id=f"seg-{self._num_segments}")
                    pcm = self._tone_tts.generate_pcm(buffered)
                    if pcm:
                        output_emitter.push(pcm)
                    output_emitter.end_segment()
                    buffered = ""
            else:
                buffered += event

        if buffered.strip():
            output_emitter.start_segment(segment_id=f"seg-{self._num_segments}")
            pcm = self._tone_tts.generate_pcm(buffered)
            if pcm:
                output_emitter.push(pcm)
            output_emitter.end_segment()

        output_emitter.end_input()


class ToneTTS(tts.TTS):
    """Local sine-wave TTS used as a speech-scheduler shim.

    Args:
        sample_rate: Output PCM sample rate.
        hz: Frequency of the generated tone.
        words_per_second: Speaking rate used to map text length to tone duration.
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        hz: int = 440,
        words_per_second: float = 2.5,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._hz = hz
        self._wps = words_per_second

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions | None = None,
    ) -> tts.ChunkedStream:
        return _ToneChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options or APIConnectOptions(),
        )

    def stream(
        self,
        *,
        conn_options: APIConnectOptions | None = None,
    ) -> tts.SynthesizeStream:
        return _ToneSynthesizeStream(
            tts=self,
            conn_options=conn_options or APIConnectOptions(),
        )

    def generate_pcm(self, text: str) -> bytes:
        """Generate a short sine-wave PCM16 buffer for ``text``."""
        words = max(1, len(text.split()))
        duration = words / self._wps
        samples = int(self._sample_rate * duration)
        amplitude = 1500
        out = bytearray()
        pack = struct.Struct("<h").pack
        for i in range(samples):
            value = int(amplitude * math.sin(2 * math.pi * self._hz * i / self._sample_rate))
            out.extend(pack(value))
        return bytes(out)
