"""Worker entrypoint for the real-time multilingual translation bridge.

Runs as a SEPARATE worker process from the support agent — ``AgentServer``
supports only one ``rtc_session`` per process:

    python -m agent.translation_worker start

Every remote participant joins audio-only (no camera is required or
subscribed). Each gets a private ``translation-<identity>`` audio feed;
utterances are transcribed, language-detected, translated, and spoken back on
every OTHER participant's feed — in English (ENGLISH_RELAY) or in each
listener's own language (NATIVE_RELAY), switchable at runtime over the
``rs.translation.control`` data topic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from livekit import agents

from .config import get_agent_settings
from .state import SessionState, StateLoadError
from .transcript_sink import TranscriptSink
from .translation.bridge import TranslationBridge
from .translation.core import RelayMode, TranslationCore
from .translation.registry import ParticipantRegistry
from .translation.synthesizer import EdgeTTSSynthesizer, LiveKitTTSSynthesizer
from .translation.translator import LiveKitTranslator

logger = logging.getLogger("translation-worker")

# The support agent's worker HTTP server (health check) binds 8081 in
# production mode; both workers cannot share it on one host. The load
# threshold matches main.py: system-CPU shedding misfires on shared hosts.
server = agents.AgentServer(
    port=int(os.environ.get("TRANSLATION_WORKER_PORT", "8081")),
    load_threshold=float(os.environ.get("TRANSLATION_LOAD_THRESHOLD", "0.95")),
)


def _build_synthesizer(settings) -> LiveKitTTSSynthesizer | EdgeTTSSynthesizer:
    if settings.translation_tts_backend == "edge":
        return EdgeTTSSynthesizer(sample_rate=16000)
    return LiveKitTTSSynthesizer(
        voices=settings.translation_voice_map,
        default_voice=settings.tts_model,
    )


@server.rtc_session(agent_name="translation-agent")
async def entrypoint(ctx: agents.JobContext) -> None:
    settings = get_agent_settings()
    meta = json.loads(ctx.job.metadata or "{}")

    # rtc_session jobs: establish the room connection explicitly (the
    # support agent gets this for free via AgentSession.start).
    await ctx.connect()

    registry = ParticipantRegistry()
    synthesizer = _build_synthesizer(settings)
    core = TranslationCore(
        registry=registry,
        translator=LiveKitTranslator(model=settings.llm_model),
        synthesizer=synthesizer,
        fallback_language=settings.translation_fallback_language,
    )
    try:
        await core.set_mode(RelayMode(settings.translation_relay_mode))
    except ValueError:
        logger.warning(
            "unknown translation_relay_mode %r; defaulting to ENGLISH_RELAY",
            settings.translation_relay_mode,
        )

    # Durable transcript via the backend session, when the job is tied to one.
    sink = None
    if meta.get("session_id"):
        try:
            state = await SessionState.load(meta["session_id"], settings)
            sink = TranscriptSink(state=state, settings=settings)
            ctx.add_shutdown_callback(sink.flush_and_close)
        except StateLoadError:
            logger.exception(
                "translation sink disabled: could not load session %s",
                meta["session_id"],
            )

    bridge = TranslationBridge(
        room=ctx.room,
        core=core,
        stt_model=settings.translation_stt_model,
        synthesizer_sample_rate=synthesizer.sample_rate,
        sink=sink,
    )
    await bridge.start()
    ctx.add_shutdown_callback(bridge.aclose)
    logger.info("translation bridge started for room %s", ctx.room.name)


def _run_worker() -> None:
    settings = get_agent_settings()
    if (
        settings.allow_degraded_start
        and (not settings.livekit_api_key or not settings.livekit_api_secret)
        and "start" in sys.argv
    ):
        logger.warning(
            "Translation worker in degraded mode: missing LiveKit credentials. "
            "The container will idle until credentials are provided."
        )
        asyncio.get_event_loop().run_forever()
    else:
        # Same pattern as agent.main: the CLI reads LiveKit connection params
        # from the environment, so export them from our pydantic settings.
        os.environ.setdefault("LIVEKIT_URL", settings.livekit_url)
        os.environ.setdefault("LIVEKIT_API_KEY", settings.livekit_api_key)
        os.environ.setdefault("LIVEKIT_API_SECRET", settings.livekit_api_secret)
        agents.cli.run_app(server)


if __name__ == "__main__":
    _run_worker()
