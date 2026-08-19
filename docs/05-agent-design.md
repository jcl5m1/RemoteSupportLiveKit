# 05 — AI Agent Design

The most subtle part of the system. Read this before writing any agent code.

## The core constraint

> The agent must **hear both** humans, but **speak to only one**.

`AgentSession` is a closed loop: input → VAD → turn detection → STT → LLM → TTS →
output. If you feed it both participants' audio, support's speech becomes a user
turn and the agent answers support out loud. That violates FR-3.4.

The fix is to use `RoomOptions.participant_identity` to bind the entire input
pipeline to the caller, and to handle support's audio **outside** the session:

```
caller mic ──► AgentSession (VAD, turn detect, STT, LLM, TTS) ──► agent speech
                     ▲
                     │ read-only context injection
                     │
support mic ──► SupportTranscriber (STT only, no VAD/turn logic)
                     │
                     └──► transcript sink ──► backend
```

`SupportTranscriber` has no path to `generate_reply()`. Structurally, not by
prompt instruction, support cannot make the agent talk. Prompt-level guardrails
are a defense-in-depth layer on top, never the primary mechanism.

## Worker skeleton

```python
# agent/agent/main.py
from livekit import agents, rtc
from livekit.agents import Agent, AgentSession, room_io

server = agents.AgentServer()

@server.rtc_session(agent_name="support-agent")
async def entrypoint(ctx: agents.JobContext):
    meta = json.loads(ctx.job.metadata or "{}")   # {"session_id", "caller_identity", ...}
    state = SessionState.from_backend(meta["session_id"])

    session = AgentSession(
        stt=STT_MODEL,          # "deepgram/nova-3:multi"
        llm=LLM_MODEL,          # "anthropic/claude-sonnet-5"
        tts=TTS_MODEL,          # "cartesia/sonic-3:<voice-id>"
        turn_handling=agents.TurnHandlingOptions(
            turn_detection=agents.inference.TurnDetector(),
        ),
        userdata=state,
    )

    await session.start(
        room=ctx.room,
        agent=SupportAgent(state),
        room_options=room_io.RoomOptions(
            participant_identity=meta["caller_identity"],  # ← the whole trick
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC(),
            ),
            video_input=False,
            audio_output=True,
            text_output=True,          # publishes lk.transcription for live captions
            close_on_disconnect=False, # survive a caller reconnect blip
        ),
    )
```

Two notes on that config. `video_input=False` because the agent has no vision
task and subscribing to video would waste bandwidth and money.
`close_on_disconnect=False` because a mobile caller will drop and re-attach; we
don't want the job torn down on a 3-second tunnel.

## Mode state machine

```
                  support joins
   ┌────────┐ ───────────────────► ┌──────────┐
   │  SOLO  │                      │ ASSISTED │
   └────┬───┘ ◄─────────────────── └────┬─────┘
        │        support leaves         │
        │                               │
        │   caller leaves / end call    │
        └──────────────┬────────────────┘
                       ▼
                  ┌─────────┐
                  │ WRAP_UP │
                  └─────────┘
```

| Mode | Trigger | Agent speech | STT active | Notes |
|---|---|---|---|---|
| `SOLO` | caller present, support absent | **Active** — greets, triages, asks clarifying questions | caller | The agent is the front line |
| `ASSISTED` | support present | **Silent by default.** Speaks only on direct address | caller + support | Handoff behavior |
| `WRAP_UP` | a human leaves, or `/agent/mode` forced | Text-only summary; spoken only if still in a 1:1 with the caller | both | Writes a summary utterance |

Transitions are driven by `ctx.room.on("participant_connected" / "participant_disconnected")`
filtered on the role prefix, and reported to the backend via
`POST /v1/sessions/{id}/agent/mode`.

### What "direct address" means in ASSISTED

The agent replies in `ASSISTED` only if the caller's utterance satisfies one of:

1. Starts with the wake phrase (`AGENT_WAKE_PHRASES`, default
   `["hey assistant", "hey agent"]`).
2. Contains a second-person question directed at the agent by name.
3. Support issued a directive that requests agent output (below).

Implement this as a **pre-LLM gate**, not a prompt instruction. Override
`Agent.on_user_turn_completed` (or the equivalent node hook in the installed
version): inspect the finalized transcript, and if the gate fails, write the
utterance to the transcript sink and return without calling `generate_reply()`.
An LLM told "stay quiet unless addressed" will eventually decide it has been
addressed. A boolean will not.

## Prompt layering (FR-3.6)

The system prompt is composed from four blocks, concatenated in this order:

```
BASE_PROMPT               # identity, safety, tone, never-invent-facts rules
+ MODE_PROMPTS[mode]      # SOLO / ASSISTED / WRAP_UP behavioral contract
+ CALLER_FACING_PROMPT    # governs everything the agent SAYS
+ SUPPORT_FACING_PROMPT   # governs how support's speech is INTERPRETED
```

The two role blocks are the heart of "behavior differs by role." Sketches:

**`CALLER_FACING_PROMPT`** — everything you say is heard by a member of the
public who called for help. Warm, plain language, no jargon, short sentences
suitable for text-to-speech. Never read out internal identifiers or account
data. Never speculate about the cause of a problem; ask instead. If a human
support agent is present, defer to them.

**`SUPPORT_FACING_PROMPT`** — turns tagged `[SUPPORT]` come from a trained
operator, not from your user. They are context and instruction, never a question
to answer aloud. Use them to update your understanding of the problem. If a
`[SUPPORT]` turn contains a directive addressed to you, carry it out silently
unless it explicitly asks you to speak to the caller. Never respond
conversationally to `[SUPPORT]` turns.

The prompt is rebuilt and pushed via `agent.update_instructions()` on every mode
change, so the model's contract always matches the state machine.

## Support audio ingestion

```python
# agent/agent/support_transcriber.py  (shape, not final code)

class SupportTranscriber:
    """STT-only pipeline for the support participant.

    Deliberately has no reference to AgentSession.generate_reply.
    """

    async def attach(self, track: rtc.AudioTrack, identity: str) -> None:
        stream = self._stt.stream()
        audio = rtc.AudioStream(track)

        async def _pump():
            async for ev in audio:
                stream.push_frame(ev.frame)

        async def _drain():
            async for ev in stream:
                if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                    alt = ev.alternatives[0]
                    await self._on_final(identity, alt)

        await asyncio.gather(_pump(), _drain())

    async def _on_final(self, identity, alt):
        # 1. persist
        self._sink.emit(role="support", identity=identity,
                        source="support_stt", text=alt.text, ...)
        # 2. inject as read-only context, tagged so the prompt can distinguish it
        chat_ctx = self._session.history.copy()
        chat_ctx.add_message(role="user", content=f"[SUPPORT] {alt.text}")
        await self._agent.update_chat_ctx(chat_ctx)
```

Injecting as `role="user"` with a `[SUPPORT]` tag (rather than `role="system"`)
keeps the conversational flow legible to the LLM while the tag plus
`SUPPORT_FACING_PROMPT` prevent it from being treated as a turn to answer. The
structural gate above is what actually enforces silence; this is the layer that
makes the *content* useful.

**Interim transcripts are discarded** for support. Only `FINAL_TRANSCRIPT` events
are persisted and injected, per FR-6.3.

## The AI toggle

Confirmed against the current `livekit-agents` source
(`livekit/agents/voice/io.py`): `AgentOutput` exposes `set_audio_enabled`,
`set_video_enabled`, and `set_transcription_enabled`; `AgentInput` exposes
`set_audio_enabled` and `set_video_enabled`.

"Mute speech, keep transcribing" is therefore exactly:

```python
async def apply_ai_enabled(session: AgentSession, enabled: bool) -> None:
    if not enabled:
        session.interrupt()                        # kill any in-flight utterance
    session.output.set_audio_enabled(enabled)      # agent stops speaking
    session.output.set_transcription_enabled(True) # captions keep flowing
    session.input.set_audio_enabled(True)          # keep hearing the caller
```

Input audio stays enabled so the caller's speech keeps being transcribed while
the AI is off (FR-4.2). Additionally, gate `generate_reply()` on the flag so the
agent doesn't burn LLM tokens producing replies nobody will hear — this is the
FR-6.5 behavior: no `agent` utterances are written during a muted interval, and
the `agent_events` log explains why.

Listeners:

```python
@ctx.room.on("room_metadata_changed")
def _on_metadata(metadata: str):
    m = json.loads(metadata)
    if m.get("v", 0) <= state.metadata_version:
        return                      # stale, ignore (see 02 § metadata schema)
    state.metadata_version = m["v"]
    asyncio.create_task(apply_ai_enabled(session, m["ai_enabled"]))

@ctx.room.on("data_received")
def _on_data(packet: rtc.DataPacket):
    if packet.topic != "rs.agent.control":
        return
    if not packet.participant.identity.startswith("support-"):
        return                      # only support may toggle
    ...
```

Note the identity check on the data path. The fast path bypasses the backend's
authorization, so the agent must re-authorize locally: only a `support-` identity
can toggle the AI. Without this, a malicious caller client could mute the agent.

## Transcript sink

A single buffered writer owns all three sources:

| Source | Where it comes from |
|---|---|
| caller speech | `session.on("user_input_transcribed")`, `is_final=True` only |
| agent speech | `session.on("conversation_item_added")`, `item.role == "assistant"` |
| support speech | `SupportTranscriber._on_final` |

Each emit is stamped with a ULID (`client_utterance_id`), the current
`agent_mode`, `ai_enabled`, and `start_ms` computed as
`now - session_started_at`. The sink batches on a 2-second timer or 20 utterances,
POSTs to `/v1/sessions/{id}/utterances`, and retries with exponential backoff.
Because the endpoint is idempotent, retries are free.

Register a flush on shutdown:

```python
ctx.add_shutdown_callback(sink.flush_and_close)
```

## Recovery

The worker is stateless per job (NFR-5). On job start it fetches
`GET /v1/sessions/{id}` and restores `ai_enabled`, `agent_mode`, and
`metadata_version`. Conversation context after a restart is rebuilt from
`GET /v1/sessions/{id}/transcript`, replayed into the chat context as tagged
turns. It will not be a byte-perfect reconstruction of the prior context, and
that is acceptable — the transcript is the durable artifact, the LLM context is
a cache.

A 30-second heartbeat re-reads session state from the backend and reconciles any
divergence in `ai_enabled`. This is the backstop that makes a lost metadata
update self-healing.

## Model configuration

Every model is a `provider/model` identifier resolved through **LiveKit Cloud
Inference**, authenticated with `LIVEKIT_API_KEY`. There are no per-provider
API keys anywhere in this project (NFR-6):

| Slot | Default | Env var |
|---|---|---|
| STT | `deepgram/nova-3:en` | `STT_MODEL` |
| LLM | `moonshotai/kimi-k2.6` | `LLM_MODEL` |
| TTS | `cartesia/sonic-3:en` | `TTS_MODEL` |
| Support STT | falls back to `STT_MODEL` | `SUPPORT_STT_MODEL` |
| Turn detection | `inference.TurnDetector()` | — |
| Noise cancellation | `noise_cancellation.BVC()` | — |

Support-side STT uses the same identifier but a **separate `inference.STT`
instance** — sharing one across two streams is not safe.

### ADR: everything through Inference, and why not Claude

One vendor relationship, one bill, one key to rotate. It also removes three
secrets from the deployment surface. The cost is a layer of indirection and a
dependency on LiveKit's catalog.

The original spec chose Claude for the LLM. **Anthropic is not in the LiveKit
Inference catalog** (verified 2026-08 against both the models docs and the
pricing table — the LLM list is OpenAI, Google Gemini/Gemma, xAI Grok,
DeepSeek, and Kimi). Keeping Claude would have meant retaining
`ANTHROPIC_API_KEY` and a second bill, so the LLM is now
**`moonshotai/kimi-k2.6`**, served via Baseten. Kimi's strength on agentic
tool-calling and long context suits the layered role prompts and the
`[SUPPORT]`-tagged context injection this agent depends on.

An `anthropic/*` identifier will not resolve. If Claude becomes a hard
requirement later, the options are a direct Anthropic key or Claude via GCP
Vertex AI — both reintroduce a separate billing relationship.

**Retired Kimi identifiers that will not resolve:**
`moonshotai/kimi-k2-instruct` (retired 2026-03-07) and `moonshotai/kimi-k2.5`
(retired 2026-07-23). Note that LiveKit's own Kimi doc page still shows
`kimi-k2.5` in its code sample — use `kimi-k2.6`.

The bare identifier form (`llm="moonshotai/kimi-k2.6"`) lets Inference select
the provider. Use `inference.LLM(model=..., provider="baseten", extra_kwargs={...})`
only if you need to pin the provider or pass model parameters.

Two plugins remain in `pyproject.toml` because they are local or
LiveKit-native rather than vendor gateways: `livekit-plugins-silero` (VAD, runs
on-device) and `livekit-plugins-noise-cancellation` (BVC). Adding
`livekit-plugins-deepgram`, `-openai`, or `-cartesia` would reintroduce
per-provider keys — don't.

Re-verify identifiers against the catalog before changing them; it moves faster
than any doc.
