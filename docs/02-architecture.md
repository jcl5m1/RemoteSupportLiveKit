# 02 — Architecture

## Components

```
┌────────────────┐         ┌────────────────┐
│  Flutter app   │         │  Flutter app   │
│  role: caller  │         │  role: support │
└───────┬────────┘         └───────┬────────┘
        │  HTTPS (REST)            │  HTTPS (REST)
        │                          │
        │        ┌─────────────────▼──────────────────┐
        └───────►│   Backend — FastAPI (Python 3.12)  │
                 │  • token minting (scoped JWT)      │
                 │  • join codes / session registry   │
                 │  • agent dispatch                  │
                 │  • egress orchestration            │
                 │  • transcript ingest + export      │
                 │  • LiveKit webhook receiver        │
                 └───┬──────────┬─────────────┬───────┘
                     │          │             │
              ┌──────▼───┐  ┌───▼────┐   ┌────▼─────┐
              │ Postgres │  │  GCS   │   │ LiveKit  │
              │          │  │ bucket │   │  Cloud   │
              └──────────┘  └────▲───┘   └────┬─────┘
                                 │            │ WebRTC
                          Egress │            │
                          writes │   ┌────────▼─────────┐
                                 └───┤  LiveKit Egress  │
                                     └──────────────────┘
                                              │
                 ┌────────────────────────────▼───────┐
                 │  Agent worker (livekit-agents 1.6) │
                 │  • AgentSession ← caller track     │
                 │  • support STT stream (parallel)   │
                 │  • mode state machine              │
                 │  • transcript POST → backend       │
                 └────────────────────────────────────┘
```

Four deployable units: **backend**, **agent worker**, **Flutter app**, and
managed LiveKit Cloud. Postgres and GCS are managed dependencies.

## Identity and naming conventions

| Thing | Format | Example |
|---|---|---|
| `session_id` | UUIDv4 | `9f1c…` |
| `room_name` | `rs_{session_id}` | `rs_9f1c8a20-…` |
| caller identity | `caller-{8 hex}` | `caller-3af91b0c` |
| support identity | `support-{user_id}` | `support-u_412` |
| agent identity | `agent` | `agent` |
| agent_name (dispatch) | `support-agent` | — |
| join code | 6× Crockford base32 minus `ILOU` | `K7R2XM` |

Role is carried in three places, deliberately redundant:

1. **Identity prefix** — cheap, works even before metadata syncs.
2. **Token `metadata`** — `{"role":"caller","session_id":"…"}`, signed, so it is
   authoritative and unforgeable.
3. **Participant attributes** — `role`, `display_name`; convenient for UI binding
   and mutable during the call.

The agent trusts (2). Clients render from (3). Never trust a client-supplied role.

## Token grants per role

| Grant | caller | support | agent |
|---|---|---|---|
| `room_join` | ✓ | ✓ | ✓ |
| `room` | session room | session room | session room |
| `can_publish` | ✓ | ✓ | ✓ (audio only) |
| `can_subscribe` | ✓ | ✓ | ✓ |
| `can_publish_data` | ✓ | ✓ | ✓ |
| `can_update_own_metadata` | ✗ | ✗ | ✗ |
| `room_admin` | ✗ | ✗ | ✗ |
| TTL | 15 min (auto-refresh) | 15 min | worker-managed |

Room metadata mutation is done **server-side only**, via the backend's API-key
credentials. No client gets `room_admin`.

## Call lifecycle

```
caller app                backend              LiveKit Cloud          agent worker
    │                        │                        │                     │
    │ POST /v1/sessions      │                        │                     │
    ├───────────────────────►│ insert session(pending)│                     │
    │                        │ mint join_code         │                     │
    │◄───────────────────────┤ {session_id, code,     │                     │
    │  consent_required=true │  consent_text, qr}     │                     │
    │                        │                        │                     │
    │ POST /consent {accept} │                        │                     │
    ├───────────────────────►│ insert consent_event   │                     │
    │                        │ create_room()          │                     │
    │                        ├───────────────────────►│                     │
    │                        │ create_dispatch(       │                     │
    │                        │   agent_name, room,    │                     │
    │                        │   metadata)            │                     │
    │                        ├───────────────────────►├────────────────────►│
    │◄───────────────────────┤ {token, ws_url}        │                     │ job accepted
    │                        │                        │                     │
    │ connect + publish      │                        │                     │
    ├────────────────────────┼───────────────────────►│                     │
    │                        │                        │  participant_joined │
    │                        │◄── webhook ────────────┤                     │
    │                        │                        │  AgentSession.start │
    │                        │                        │◄────────────────────┤
    │                        │                        │   linked=caller     │
    │                        │◄── webhook ────────────┤                     │
    │                        │  track_published ×2    │                     │
    │                        │  start_track_egress ×2 │                     │
    │                        ├───────────────────────►│                     │
    │◄─── agent greets (SOLO mode) ───────────────────┼─────────────────────┤
    │                        │                        │                     │
support app                  │                        │                     │
    │ POST /join {code}      │                        │                     │
    ├───────────────────────►│ verify auth + code     │                     │
    │◄───────────────────────┤ {token}                │                     │
    │ connect + publish      │                        │                     │
    ├────────────────────────┼───────────────────────►│                     │
    │                        │◄── track_published ×2 ─┤                     │
    │                        │  start_track_egress ×2 │                     │
    │                        │  start_room_composite  │                     │
    │                        ├───────────────────────►│                     │
    │                        │                        │  participant_joined │
    │                        │                        ├────────────────────►│
    │                        │                        │   mode → ASSISTED   │
    │                        │                        │   attach support STT│
    │                        │                        │                     │
    │  ... conversation; agent posts utterances to backend continuously ... │
    │                        │                        │                     │
    │ hang up                │                        │                     │
    ├────────────────────────┼───────────────────────►│                     │
    │                        │◄── room_finished ──────┤                     │
    │                        │  stop egress (implicit)│                     │
    │                        │  export transcript→GCS │                     │
    │                        │  session → completed   │                     │
```

## The control plane (AI toggle)

The toggle must be reliable, auditable, and survive an agent restart. It must
also feel instant. Those pull in opposite directions, so we run both paths:

**Authoritative path** (source of truth):

```
support app ──HTTPS──► POST /v1/sessions/{id}/agent {enabled}
                          │
                          ├─► UPDATE sessions SET ai_enabled = ?
                          ├─► INSERT agent_events(...)
                          └─► RoomService.update_room_metadata(room, json)
                                       │
                                       └─► agent worker receives
                                           room.on("room_metadata_changed")
                                           → session.output.set_audio_enabled(x)
```

**Fast path** (latency hedge):

```
support app ──data msg (topic "rs.agent.control")──► agent worker
              reliable=true                          → applies immediately
```

The agent applies whichever arrives first and treats them as idempotent. Room
metadata is reconciled on every agent reconnect, so if the data message is lost
or the worker restarted, metadata wins. The support UI renders optimistically and
confirms against the metadata echo; if no echo arrives within 2 s it reverts and
shows an error.

**Why not RPC?** `performRpc` would work, but it targets a specific participant
identity and fails if the agent has momentarily reconnected. It also leaves no
server-side record. Room metadata is durable, observable by both clients (so the
caller's indicator updates for free), and reconciles automatically. Flutter RPC
support also lags the other SDKs — see [10](10-risks-references.md).

## Room metadata schema

Set by the backend, read by everyone:

```json
{
  "session_id": "9f1c8a20-...",
  "ai_enabled": true,
  "recording": true,
  "mode": "ASSISTED",
  "v": 7
}
```

`v` is a monotonically increasing version. Clients and the agent ignore any
metadata whose `v` is lower than the last one they applied — this prevents
out-of-order metadata delivery from resurrecting a stale toggle state.

## Deployment

| Unit | Runtime | Scaling |
|---|---|---|
| backend | Cloud Run / any container host, 2+ instances | stateless, horizontal |
| agent worker | LiveKit Cloud Agents *or* container with outbound WS | one job per session; worker handles N jobs |
| Postgres | Cloud SQL | single primary |
| GCS | one bucket, uniform bucket-level access | — |

Webhooks require the backend to be publicly reachable at
`POST /v1/webhooks/livekit`, with signature verification enabled.

## Web support client & regression harness

For automated regression testing the system also exposes a minimal browser-based
support client. It is not a production user interface; it exists so the call
path can be exercised headlessly without two physical mobile devices.

```
┌─────────────────┐      create session       ┌──────────────────┐
│  Test harness   │ ────────────────────────▶ │  GCP backend     │
│  (Playwright)   │                           │                  │
└────────┬────────┘                           │  /support-web/   │
         │                                    │  /internal/test/ │
         │ 1. open caller page   2. open support page  support-token│
         │    ( LiveKit JS SDK ) ( LiveKit JS SDK )    │
         ▼                                                 ▼
┌─────────────────┐                           ┌──────────────────┐
│  Caller client  │ ◀──── audio/video ──────▶ │  Support client  │
│  (browser tab)  │      (via LiveKit Cloud)  │  (browser tab)   │
└─────────────────┘                           └──────────────────┘
```

**`backend/app/static/support-web/`**
Plain HTML + JavaScript served under `/support-web/`. It joins a room given
`?token=<jwt>&ws_url=<wss://...>`, publishes fake camera/mic (via Chromium's
`--use-fake-device-for-media-stream`), subscribes to remote tracks, and exposes
`window.testState` flags (`connected`, `remoteVideoReady`, `remoteAudioReady`)
for the harness to poll.

**`POST /internal/test/support-token`**
Service-key-gated endpoint available only when `ALLOW_TEST_ENDPOINTS=true`.
The harness calls it after creating a caller session and recording consent; it
returns a support LiveKit token using a synthetic identity (`support-test-support`)
so the test does not need Firebase Auth.

**`tests/e2e/test_two_web_clients.py`**
Playwright + pytest flow:
1. `POST /v1/sessions` (caller)
2. `POST /v1/sessions/{id}/consent` (caller)
3. `POST /internal/test/support-token` (service key)
4. Open two Chromium tabs at `/support-web/?token=...&ws_url=...`
5. Wait for both to connect and show remote video/audio
6. Poll `RTCPeerConnection.getStats()` and assert inbound bytes increase
7. `POST /v1/sessions/{id}/end` and assert cleanup

The test runs nightly in GitHub Actions (`.github/workflows/e2e.yml`) against the
live backend, and can be triggered manually via `workflow_dispatch`.

### Expanded headless regression coverage

`tests/e2e/test_two_web_clients.py` now covers:

| Test | What it checks |
|---|---|
| `test_two_web_clients` | Two headless Chromium tabs connect, subscribe to each other's video/audio, and inbound bytes increase. |
| `test_lifecycle_reconnect` | Caller network is dropped for 5 s; the tab reconnects and media resumes. |
| `test_agent_joins_and_greets` | The AI agent joins after caller consent and produces an `agent_llm` transcript utterance. |
| `test_caller_speech_agent_response` | The caller publishes real speech audio (`fixtures/caller_prompt.wav`); the agent transcribes it (`agent_stt`) and replies (`agent_llm`). |
| `test_recordings_and_transcript_after_session_end` | Ends the call and verifies track/room-composite egress recordings plus JSONL/VTT/TXT transcript exports. |

**Local vs. deployed recording tests.** Recording egress and transcript export are
driven by LiveKit Cloud webhooks (`track_published`, `egress_ended`, `room_finished`).
When the backend runs on `localhost`, LiveKit cannot deliver those webhooks, so the
recording test is skipped locally. Run it against the deployed backend (e.g.
`https://remotesupport.lgitech.net`) to verify egress end-to-end.

### Agent TTS fallback

LiveKit Cloud Inference TTS currently fails in this project with
`no audio frames were pushed` across providers/voices. The agent therefore defaults
to `USE_DUMMY_TTS=true`: a local sine-wave `ToneTTS` shim that satisfies the speech
scheduler so text replies still flow to the transcript sink. The audio is not
intelligible speech, but it keeps the dialogue loop functional for regression
testing. Set `USE_DUMMY_TTS=false` to re-test cloud TTS once the provider issue is
resolved.
