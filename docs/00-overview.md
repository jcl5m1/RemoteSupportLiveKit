# 00 — Overview & Decision Record

## What this system is

A two-party mobile audio/video support calling app built on LiveKit, with a third
AI participant that listens to the whole conversation but only ever speaks to one
of the two humans.

Three roles, fixed for the life of a session:

| Role | Who | Publishes | Hears | Spoken to by agent |
|---|---|---|---|---|
| `caller` | End user needing help. Anonymous, joins with a 6‑char code. | camera + mic | support + agent | **yes** |
| `support` | Support operator. Authenticated. | camera + mic | caller + agent | **no** |
| `agent` | AI voice participant. Server-dispatched. | mic (TTS) only | caller + support | — |

The agent hears both humans. It **responds** only to the caller. Support's speech
is ingested as read-only context and as transcript material, never as a turn the
agent should answer aloud.

## Locked decisions

These were decided up front and are not open questions for the implementing agent.

| Area | Decision |
|---|---|
| Mobile client | **Flutter** (single codebase, both roles in one app behind a role switch) |
| LiveKit hosting | **LiveKit Cloud** (managed SFU + Egress + Agents) |
| Backend | **Python 3.12 + FastAPI** (shares a language with the agent worker) |
| Object storage | **Google Cloud Storage** |
| Database | **PostgreSQL** (sessions, transcripts, recordings metadata) |
| Agent hearing | Hears both; `AgentSession` linked to caller for turn-taking; support on a parallel STT-only stream |
| Agent behavior differentiation | **Separate system prompt per role**, layered on a call-state mode |
| Agent speech policy | Active before support joins; drops to silent-listener once support is present |
| AI toggle (support UI) | **Mute speech, keep transcribing** — `session.output.set_audio_enabled(False)` |
| Room identity | Internal UUID + short **6-char Crockford base32 join code**, plus QR and deep link |
| Auth | Caller anonymous (code + rate limit); support authenticated |
| AI stack | Deepgram STT · Kimi K2.6 LLM · Cartesia TTS — **all via LiveKit Cloud Inference**, no per-provider keys |
| Recording layout | **4× Track Egress** (caller.video, caller.audio, support.video, support.audio) **+ 1× Room Composite** MP4 |
| Consent | **Blocking gate** — no LiveKit token is issued to the caller until consent is recorded |
| Transcripts | Postgres rows (live) + JSONL/VTT/TXT export to GCS at session end |
| Retention | GCS lifecycle rules + explicit delete endpoint |

## Why the agent design is shaped this way

The naive approach — one `AgentSession` that hears the whole room — breaks the
core requirement, because turn detection would treat support's speech as a user
turn and the agent would answer support out loud. The requirement is
*asymmetric*: symmetric input, asymmetric output.

LiveKit's `RoomOptions.participant_identity` gives us exactly the asymmetry we
need. It binds the session's input pipeline (VAD, STT, turn detection,
interruption) to a single participant. Everything the agent *reacts to* comes
from the caller. Support is then handled out-of-band by a second, dumber pipeline
that only does speech-to-text and appends labelled context.

This also gives us **diarization for free**. We never run an acoustic
speaker-separation model, because speaker identity is structural: each utterance
arrives on a known participant's track. That is strictly more reliable than
diarizing a mixed-down stream, and it is why the recording layout keeps the two
audio streams separate rather than muxing them.

## Document map

| Doc | Contents |
|---|---|
| [01-requirements.md](01-requirements.md) | Numbered functional + non-functional requirements |
| [02-architecture.md](02-architecture.md) | Components, call lifecycle sequence, control plane |
| [03-data-model.md](03-data-model.md) | Postgres schema, enums, GCS object layout |
| [04-api-contract.md](04-api-contract.md) | Every REST endpoint, request/response shapes |
| [05-agent-design.md](05-agent-design.md) | Modes, prompt layering, dual-STT, AI toggle |
| [06-recording-transcripts.md](06-recording-transcripts.md) | Egress orchestration, transcript pipeline, export |
| [07-flutter-app.md](07-flutter-app.md) | Screens, state, deep links, QR, permissions |
| [08-security-compliance.md](08-security-compliance.md) | Auth, consent, retention, threat notes |
| [09-implementation-plan.md](09-implementation-plan.md) | 7 phases, tasks, acceptance criteria |
| [10-risks-references.md](10-risks-references.md) | Known upstream issues, version pins, links |
