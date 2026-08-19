# 01 — Requirements

Requirement IDs are stable. Tests and acceptance criteria reference them.

## FR-1 — Session establishment

- **FR-1.1** A caller can create a session from the mobile app with no account.
- **FR-1.2** Session creation returns a unique internal `session_id` (UUIDv4), a
  LiveKit `room_name` derived from it, and a human-shareable `join_code`.
- **FR-1.3** `join_code` is 6 characters of Crockford base32 with `I`, `L`, `O`, `U`
  removed (28-symbol alphabet → ~481M combinations). Generation retries on
  collision against currently-active codes.
- **FR-1.4** A join code is valid only while its session is in `pending` or
  `active` state, and expires `JOIN_CODE_TTL_SECONDS` (default 1800) after creation.
- **FR-1.5** The caller UI displays the join code as large text **and** as a QR code.
- **FR-1.6** Support can join by (a) typing the 6-char code, (b) scanning the QR,
  or (c) opening a deep link. All three resolve through the same endpoint.
- **FR-1.7** Deep links are supported in two forms: custom scheme
  `remotesupport://join?code=XXXXXX` and universal/app link
  `https://<APP_LINK_HOST>/j/XXXXXX`.
- **FR-1.8** A session admits at most one `caller` and one `support`. A second
  join attempt for an occupied role is rejected with `409 role_occupied`.
- **FR-1.9** Roles are assigned by the server, never claimed by the client.

## FR-2 — Media

- **FR-2.1** Both humans publish one camera track and one microphone track.
- **FR-2.2** Both humans see the remote video full-bleed with their own camera in
  a draggable picture-in-picture tile.
- **FR-2.3** In-call controls for both roles: mute mic, disable camera, switch
  camera, speaker/earpiece, hang up.
- **FR-2.4** Video is published with the H.264 codec so that Track Egress yields
  `.mp4` containers rather than `.ivf`. (See ADR in [06](06-recording-transcripts.md).)
- **FR-2.5** The app recovers from transient network loss using the SDK's built-in
  reconnection, and surfaces connection quality to the user.

## FR-3 — AI agent

- **FR-3.1** An AI voice agent is dispatched into the room when a session becomes
  active. Dispatch is explicit (named agent), not automatic-on-every-room.
- **FR-3.2** The agent's turn-taking, interruption, and reply generation are bound
  to the caller's audio track only.
- **FR-3.3** The agent transcribes support's audio on a separate STT stream and
  injects it into its own context labelled as support speech.
- **FR-3.4** The agent **never** produces speech in response to support's audio.
- **FR-3.5** The agent operates in modes driven by call state:
  - `SOLO` — caller present, support absent. Agent is conversationally active:
    greets, triages, collects the problem description.
  - `ASSISTED` — support present. Agent is a silent listener. It speaks only when
    the caller directly addresses it (wake phrase or a question aimed at it), or
    when support explicitly requests agent output.
  - `WRAP_UP` — a human has left or the session is ending. Agent produces a final
    summary into the transcript (text only, not spoken, unless in `SOLO`).
- **FR-3.6** The agent holds distinct system prompt blocks per role: a
  caller-facing block governing everything it says aloud, and a support-facing
  block governing how support utterances are interpreted. The composed system
  prompt = base + active-mode block + caller-facing block + support-facing block.
- **FR-3.7** Mode transitions are logged as `agent_events` rows.

## FR-4 — AI enable/disable

- **FR-4.1** The support UI has a persistent toggle for the AI agent.
- **FR-4.2** Disabling suppresses **agent speech only**. The agent stays in the
  room, keeps listening, and keeps producing transcript entries.
- **FR-4.3** Toggling takes effect in under 500 ms p95 and immediately interrupts
  any in-flight agent utterance.
- **FR-4.4** The backend is the source of truth for the toggle state; it is
  persisted on the session and survives an agent worker restart.
- **FR-4.5** Both clients see the current AI state; the caller sees it as a
  passive indicator, support as an interactive control.
- **FR-4.6** Every toggle is written to `agent_events` with actor and timestamp.

## FR-5 — Recording

- **FR-5.1** Each session produces **four independent media files**:
  `caller.video`, `caller.audio`, `support.video`, `support.audio`.
- **FR-5.2** Each session additionally produces one composited MP4 of the whole
  room for convenient human playback.
- **FR-5.3** All media is written directly from LiveKit Egress to GCS. The backend
  never proxies media bytes.
- **FR-5.4** Track egresses are started reactively, on the LiveKit `track_published`
  webhook, because track SIDs do not exist until publication.
- **FR-5.5** Egress lifecycle (`starting` → `active` → `complete`/`failed`) is
  mirrored into the `recordings` table via the `egress_updated` webhook.
- **FR-5.6** A failed egress does not terminate the call. It is logged and
  surfaced in the session record.
- **FR-5.7** Both participants see a persistent recording indicator whenever any
  egress is active.

## FR-6 — Transcripts

- **FR-6.1** Every finalized utterance from caller, support, and agent is stored
  as its own row with speaker role, participant identity, start/end offsets in
  milliseconds relative to session start, and text.
- **FR-6.2** Speaker attribution is structural (derived from the source track),
  not acoustic.
- **FR-6.3** Interim (non-final) transcriptions are streamed to the clients for
  live captions but are **not** persisted.
- **FR-6.4** Agent replies are stored with role `agent`, including replies
  generated while the agent is muted (so the record shows what it would have said
  — see FR-6.5 for the distinction).
- **FR-6.5** When the AI is disabled the agent does not generate replies at all,
  so no `agent` rows are written for that interval. The disable/enable boundary is
  visible in `agent_events`, making transcript gaps explainable.
- **FR-6.6** At session end the transcript is exported to GCS as
  `transcript.jsonl` (canonical), `transcript.vtt` (captions), and
  `transcript.txt` (human-readable), alongside the media.
- **FR-6.7** Support can view a live scrolling transcript panel during the call.

## FR-7 — Consent & retention

- **FR-7.1** Recording consent is a **blocking gate**: the backend does not issue
  the caller's LiveKit token until a consent decision is recorded. This means
  there is no unrecorded audio at the head of the call.
- **FR-7.2** Consent decisions are stored with timestamp, IP, and the exact
  consent-text version shown.
- **FR-7.3** Declining ends the session by default. If
  `ALLOW_UNRECORDED_FALLBACK=true`, the session proceeds with all egress disabled
  and the session flagged `recording_declined`.
- **FR-7.4** GCS lifecycle rules delete session media after `RETENTION_DAYS`
  (default 30).
- **FR-7.5** `DELETE /v1/sessions/{id}/data` purges media objects, transcript
  rows, and transcript exports for one session, leaving a tombstone audit row.

## Non-functional

- **NFR-1** End-to-end audio latency between the two humans ≤ 300 ms p95 on a
  normal mobile network.
- **NFR-2** Agent first-token-to-speech latency ≤ 1.2 s p95 in `SOLO` mode.
- **NFR-3** Join-code entry to media flowing ≤ 5 s p95.
- **NFR-4** No secrets (LiveKit API secret, provider keys, GCS credentials) ever
  reach a mobile client. Clients receive only short-lived scoped JWTs.
- **NFR-5** The agent worker is stateless per job; all durable state lives in
  Postgres. Restarting the worker mid-call degrades gracefully (see
  [05](05-agent-design.md) § Recovery).
- **NFR-6** Every AI provider (STT/LLM/TTS) is selected by config string, so
  swapping vendors requires no code change.
- **NFR-7** Structured JSON logs with `session_id` on every line, across backend
  and agent.
