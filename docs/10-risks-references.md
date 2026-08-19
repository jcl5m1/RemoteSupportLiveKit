# 10 — Risks & References

## Known upstream issues to verify before building on them

These were open at the time of writing (August 2026). Check each before relying
on the affected behavior.

### Transcription text streams for multiple participants
[livekit/agents#3657](https://github.com/livekit/agents/issues/3657) — transcription
text streams reported as not generated for more than one participant.

**Impact:** live captions on the client may only show one speaker.
**Mitigation:** already designed around. The durable transcript is the one the
agent POSTs to the backend, where attribution is structural. Client text streams
are display-only. If the issue bites, render support captions from the backend
polling path instead.

### Transcription events attributed to the agent
[livekit/agents#3477](https://github.com/livekit/agents/issues/3477) —
transcription events show the agent's identity instead of the actual speaker's.

**Impact:** client-side speaker labels could be wrong.
**Mitigation:** same as above — never derive speaker identity from the client
stream. The `lk.transcribed_track_id` attribute is a more reliable client-side
signal than sender identity if you need one.

### Egress A/V drift
[Community report](https://community.livekit.io/t/drift-between-audio-and-video-in-the-participant-recording-egress-livekit-cloud/648)
of drift in participant recording egress on LiveKit Cloud.

**Impact:** long calls may show audio/video misalignment in per-participant output.
**Mitigation:** the room composite is the human-review artifact; separate tracks
are archival. If drift matters, realign in post using transcript timestamps.
Measure drift on a 30-minute test call during phase 4 before assuming it's fine.

### Flutter RPC support
[livekit/client-sdk-flutter#694](https://github.com/livekit/client-sdk-flutter/issues/694) —
`registerRpcMethod` requested on `LocalParticipant`. Flutter is absent from the
RPC support table in the docs, though an
[rpc-demo](https://github.com/livekit-examples/flutter-examples/blob/main/packages/rpc-demo/lib/main.dart)
exists.

**Impact:** none, by design. The AI toggle uses room metadata + data messages
specifically to avoid depending on RPC. Do not "simplify" it to RPC later
without re-checking this.

### API surface drift
`livekit-agents` has moved quickly: `WorkerOptions`/`entrypoint` →
`AgentServer`/`@server.rtc_session`, `RoomInputOptions` → `room_io.RoomOptions`,
and inline `inference.STT(...)` → model-identifier strings. The snippets in
[05](05-agent-design.md) reflect the 1.6.x line.

**Mitigation:** pin exact versions, and before writing agent code run
`python -c "import livekit.agents; help(livekit.agents.AgentSession)"` against
the pinned version to confirm the signatures. Same for `livekit.api` egress
request classes.

## Design risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Agent responds to support despite the design | Low | **Critical** — violates the core requirement | Structural separation (support has no path to `generate_reply`) + prompt guardrails + explicit acceptance test 2.3 |
| Direct-address gate too strict — agent never responds when wanted | Medium | Medium | Log every gate rejection with the utterance; tune the wake phrase list from real data |
| Direct-address gate too loose — agent interjects during a support call | Medium | High | Start strict (wake phrase only), loosen deliberately |
| Track egress produces `.ivf` because a client ignored the codec preference | Medium | Medium | Read the real filename from `EgressInfo`; alert on any non-mp4 video output |
| Egress cost surprises | Medium | Medium | 5 egresses per session is not free. Model the cost per call-minute in phase 0 and set a budget alert |
| Support STT doubles STT spend | High | Low | Expected and accepted; it's the price of dual transcription. Consider a cheaper model for the support stream |
| Transcript POST backlog on a network blip | Medium | Low | Buffered sink + idempotent endpoint + shutdown flush |
| Mobile background audio kills the call | Medium | High | Android foreground service, iOS background audio mode; test explicitly with the app backgrounded |
| Join code collision | Very low | Low | Partial unique index makes collision a retry, not a bug |
| Two-party consent jurisdiction | Medium | High (legal) | Support-side acknowledgement at sign-in; counsel review |

## Cost model to build in phase 0

Per 10-minute session, estimate and record actuals for:

- LiveKit participant minutes (3 participants × 10 min)
- Egress minutes (5 egresses × 10 min) — usually the largest line item
- STT minutes (caller + support = 2× conversation length)
- LLM tokens (varies with mode; `ASSISTED` is cheap because the gate suppresses
  most generations, but context still grows with injected support turns)
- TTS characters (only what the agent actually speaks)
- GCS storage + egress bandwidth

The `ASSISTED`-mode gate is a significant cost control, not just a behavioral
one: suppressing generation before the LLM call (rather than generating and
discarding) is what keeps a long human-to-human call from costing LLM tokens.

## References

**LiveKit docs**
- [Voice AI quickstart](https://docs.livekit.io/agents/start/voice-ai/)
- [Agent session](https://docs.livekit.io/agents/build/sessions/) — `RoomOptions`, `participant_identity`, session events
- [Agent dispatch](https://docs.livekit.io/agents/build/dispatch/) — `create_dispatch`, `CreateAgentDispatchRequest`
- [Text and transcriptions](https://docs.livekit.io/agents/multimodality/text/) — `lk.transcription` topic and attributes
- [Egress overview](https://docs.livekit.io/transport/media/ingress-egress/egress/) — egress types
- [Remote procedure calls](https://docs.livekit.io/transport/data/rpc/) — SDK support table
- [`room_io` API reference](https://docs.livekit.io/reference/python/v1/livekit/agents/voice/room_io/index.html)

**Source**
- [`livekit/agents` voice/io.py](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/voice/io.py) — `AgentOutput.set_audio_enabled`, `set_transcription_enabled`, `AgentInput.set_audio_enabled`. Verified directly; this is what the AI toggle is built on.
- [`livekit/protocol` livekit_egress.proto](https://github.com/livekit/protocol/blob/main/protobufs/livekit_egress.proto) — authoritative field names for `TrackEgressRequest`, `DirectFileOutput`, `GCPUpload`
- [`livekit/egress`](https://github.com/livekit/egress)

**SDKs**
- [`livekit-agents` (PyPI)](https://pypi.org/project/livekit-agents/) — 1.6.10 as of 2026-08-13
- [`livekit_client` (pub.dev)](https://pub.dev/packages/livekit_client) — 2.11.0
- [`client-sdk-flutter`](https://github.com/livekit/client-sdk-flutter)

## Version pins

Pin these and revisit deliberately. The ecosystem moves fast enough that an
unpinned build will break.

```
livekit-agents  == 1.6.10
livekit-api     == <match agents' transitive pin>
livekit         == <match agents' transitive pin>
livekit_client  ^2.11.0
python           3.12
```
