"""LiveKit agent worker entrypoint. Design: docs/05-agent-design.md.

The one thing to keep straight while working in this file:

    The agent HEARS both humans but SPEAKS to only the caller.

That asymmetry is enforced *structurally*, not by prompting:

  * ``AgentSession`` is bound to the caller via
    ``RoomOptions.participant_identity``. Its VAD, turn detection, STT and
    reply generation only ever see the caller's audio.
  * Support audio is handled by ``SupportTranscriber``, which has no reference
    to ``generate_reply``. There is no code path from support speech to agent
    speech.

Prompt instructions in prompts.py are defence in depth on top of that. Do not
let them become the primary mechanism -- an LLM told "stay quiet" will
eventually decide it has been addressed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, llm, room_io

from .config import get_agent_settings
from .control import ControlPlane
from .dummy_tts import ToneTTS
from .prompts import compose_instructions, is_direct_address
from .state import AgentMode, SessionState
from .support_transcriber import SupportTranscriber
from .transcript_sink import TranscriptSink

logger = logging.getLogger("support-agent")

server = agents.AgentServer()


class SupportAgent(Agent):
    def __init__(self, state: SessionState, sink: TranscriptSink) -> None:
        self._state = state
        self._sink = sink
        super().__init__(instructions=compose_instructions(state.mode))

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """The direct-address gate (FR-3.5, docs/05 § direct address).

        This runs on the CALLER's finalized turn only -- support never reaches
        here. In ASSISTED mode the agent stays silent unless the caller has
        actually addressed it.

        Suppressing generation *before* the LLM call, rather than generating and
        discarding, is what keeps a long human-to-human call from costing LLM
        tokens. It is a cost control as much as a behavioural one.
        """
        text = new_message.text_content or ""
        self._sink.emit(
            role="caller",
            identity=self._state.caller_identity,
            source="agent_stt",
            text=text,
        )

        if not self._state.ai_enabled:
            raise agents.StopResponse()
        if self._state.mode is AgentMode.ASSISTED and not is_direct_address(
            text, wake_phrases=get_agent_settings().wake_phrases
        ):
            raise agents.StopResponse()


@server.rtc_session(agent_name="support-agent")
async def entrypoint(ctx: agents.JobContext) -> None:
    settings = get_agent_settings()
    meta = json.loads(ctx.job.metadata or "{}")

    # Reconcile durable state from the backend. The worker is stateless per job
    # (NFR-5); Postgres is the source of truth for ai_enabled and mode.
    state = await SessionState.load(meta["session_id"], settings)

    sink = TranscriptSink(state=state, settings=settings)
    ctx.add_shutdown_callback(sink.flush_and_close)

    # LiveKit Cloud Inference TTS is failing in this project with
    # ``no audio frames were pushed`` for every model/voice tried.  Use a local
    # sine-wave TTS shim by default so the speech scheduler keeps moving and
    # text replies still flow to the transcript sink.
    tts = ToneTTS() if settings.use_dummy_tts else settings.tts_model

    session = AgentSession(
        # Bare identifier strings -> resolved by LiveKit Cloud Inference,
        # authenticated with LIVEKIT_API_KEY. No per-provider keys.
        stt=settings.stt_model,
        llm=settings.llm_model,
        tts=tts,
        turn_handling=agents.TurnHandlingOptions(
            turn_detection=agents.inference.TurnDetector(),
        ),
        userdata=state,
    )

    agent = SupportAgent(state, sink=sink)
    if state.chat_context.messages:
        await agent.update_chat_ctx(state.chat_context)

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=room_io.RoomOptions(
            # THE critical line. Binds the entire input pipeline to the caller.
            participant_identity=meta["caller_identity"],
            video_input=False,        # no vision task; don't pay for video
            audio_output=True,
            text_output=False,        # still crashes if true before connected
            close_on_disconnect=False,  # survive a mobile reconnect blip
        ),
    )
    logger.info("session started for %s, mode=%s", state.session_id, state.mode.value)

    # Wire transcript capture and control plane BEFORE prompting the agent to
    # speak, otherwise the proactive greeting and early caller STT are lost.
    _wire_transcript_sources(session, state, sink)

    # Support audio: STT only, no path to generate_reply.
    support_stt = SupportTranscriber(
        session=session, agent=agent, state=state, sink=sink, settings=settings
    )
    await support_stt.watch(ctx.room)

    # AI toggle: room metadata (authoritative) + data messages (fast path).
    control = ControlPlane(session=session, agent=agent, state=state, settings=settings)
    control.attach(ctx.room)

    _wire_mode_transitions(ctx, session, agent, state, support_stt)
    await control.start_heartbeat()

    # In SOLO mode the instructions tell the agent to greet; because the input
    # pipeline is waiting on caller speech, prompt a proactive greeting now.
    if state.mode is AgentMode.SOLO:
        logger.info("generating proactive greeting")
        try:
            handle = await session.generate_reply(
                instructions="Greet the caller briefly and ask how you can help today."
            )
            logger.info("proactive greeting handle: %s", handle)
        except Exception:
            logger.exception("proactive greeting failed")


def _wire_mode_transitions(
    ctx: agents.JobContext,
    session: AgentSession,
    agent: SupportAgent,
    state: SessionState,
    support_stt: SupportTranscriber,
) -> None:
    """Drive the SOLO / ASSISTED / WRAP_UP state machine.

    Re-composing instructions on every transition keeps the model's contract in
    sync with the state machine.
    """

    async def _set_mode(mode: AgentMode) -> None:
        if state.mode == mode:
            return
        state.mode = mode
        await agent.update_instructions(compose_instructions(mode))
        await state.report_mode()

    @ctx.room.on("participant_connected")
    def _on_connect(p: rtc.RemoteParticipant):
        if p.identity.startswith("support-") and state.mode is not AgentMode.ASSISTED:
            state.support_identity = p.identity
            asyncio.create_task(_set_mode(AgentMode.ASSISTED))

    @ctx.room.on("participant_disconnected")
    def _on_disconnect(p: rtc.RemoteParticipant):
        if p.identity == state.caller_identity:
            asyncio.create_task(_set_mode(AgentMode.WRAP_UP))
        elif p.identity.startswith("support-") and state.mode is AgentMode.ASSISTED:
            asyncio.create_task(_set_mode(AgentMode.SOLO))


def _wire_transcript_sources(
    session: AgentSession, state: SessionState, sink: TranscriptSink
) -> None:
    """Attach the caller and agent transcript sources.

    Interims are dropped (FR-6.3). Support's source is wired inside
    SupportTranscriber.
    """

    @session.on("user_input_transcribed")
    def _on_caller_stt(ev):
        logger.debug(
            "user_input_transcribed final=%s text=%s",
            ev.is_final,
            ev.transcript[:60] if ev.transcript else "",
        )
        if ev.is_final:
            sink.emit(
                role="caller",
                identity=state.caller_identity,
                source="agent_stt",
                text=ev.transcript,
                language=ev.language,
            )

    @session.on("conversation_item_added")
    def _on_agent_llm(ev):
        item = ev.item
        logger.debug(
            "conversation_item_added type=%s role=%s",
            type(item).__name__,
            getattr(item, "role", None),
        )
        if isinstance(item, llm.ChatMessage) and item.role == "assistant":
            text = item.text_content or ""
            if text:
                sink.emit(
                    role="agent",
                    identity="agent",
                    source="agent_llm",
                    text=text,
                )


def _run_worker() -> None:
    settings = get_agent_settings()
    if (
        settings.allow_degraded_start
        and (not settings.livekit_api_key or not settings.livekit_api_secret)
        and "start" in sys.argv
    ):
        logger.warning(
            "Agent worker in degraded mode: missing LiveKit credentials. "
            "The container will idle until credentials are provided."
        )
        asyncio.get_event_loop().run_forever()
    else:
        # The livekit.agents CLI reads LiveKit connection params from environment
        # variables rather than from our pydantic settings. Export them so a
        # caller can keep credentials in agent/.env and still use `python -m agent.main start`.
        os.environ.setdefault("LIVEKIT_URL", settings.livekit_url)
        os.environ.setdefault("LIVEKIT_API_KEY", settings.livekit_api_key)
        os.environ.setdefault("LIVEKIT_API_SECRET", settings.livekit_api_secret)
        agents.cli.run_app(server)


if __name__ == "__main__":
    _run_worker()
