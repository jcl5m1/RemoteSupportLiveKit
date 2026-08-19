# 04 — API Contract

Base path `/v1`. JSON in, JSON out. All errors use the shape:

```json
{ "error": { "code": "role_occupied", "message": "…", "details": {} } }
```

## Authentication tiers

| Tier | Header | Used by |
|---|---|---|
| none | — | caller session creation (rate-limited by IP + device id) |
| caller session | `Authorization: Bearer <caller_session_jwt>` | caller, after session creation |
| support user | `Authorization: Bearer <idp_jwt>` | support app |
| service | `X-Service-Key: <SERVICE_API_KEY>` | agent worker → backend |
| webhook | `Authorization: <livekit signature>` | LiveKit → backend |

The **caller session JWT** is issued by the backend (not LiveKit), is scoped to
one `session_id`, and lives 60 minutes. It authorizes only that session's
endpoints. It is distinct from the LiveKit room token.

---

## Sessions

### `POST /v1/sessions`

Create a session. Auth: none. Rate limit: 5/min/IP, 20/hour/device.

**Request**
```json
{ "device_id": "opaque-stable-uuid", "display_name": "Sam", "locale": "en-US" }
```

**Response `201`**
```json
{
  "session_id": "9f1c8a20-...",
  "join_code": "K7R2XM",
  "join_code_expires_at": "2026-08-19T18:30:00Z",
  "deep_link": "remotesupport://join?code=K7R2XM",
  "universal_link": "https://support.example.com/j/K7R2XM",
  "qr_payload": "https://support.example.com/j/K7R2XM",
  "caller_session_token": "eyJ…",
  "consent_required": true,
  "consent_text_version": "v1.0",
  "consent_text": "This call, including video and audio from both participants, will be recorded and transcribed…"
}
```

No LiveKit token is returned here. That is the consent gate (FR-7.1).

### `POST /v1/sessions/{id}/consent`

Auth: caller session. Idempotent per session — a second call returns the
original decision with `200`.

**Request**
```json
{ "accepted": true, "consent_text_version": "v1.0" }
```

**Response `200` (accepted)** — this is the call that actually creates the room
and dispatches the agent.
```json
{
  "accepted": true,
  "session_state": "active",
  "livekit": {
    "ws_url": "wss://xyz.livekit.cloud",
    "token": "eyJ…",
    "room_name": "rs_9f1c8a20-...",
    "identity": "caller-3af91b0c",
    "expires_at": "2026-08-19T18:15:00Z"
  },
  "recording_enabled": true
}
```

**Response `200` (declined, `ALLOW_UNRECORDED_FALLBACK=false`)**
```json
{ "accepted": false, "session_state": "consent_declined", "livekit": null }
```

### `POST /v1/sessions/join`

Support joins by code. Auth: support user.

**Request**
```json
{ "join_code": "K7R2XM", "display_name": "Dana" }
```

**Response `200`** — same `livekit` block as above, with
`identity: "support-u_412"`, plus a snapshot of current session state:
```json
{
  "session_id": "9f1c8a20-...",
  "livekit": { "...": "..." },
  "ai_enabled": true,
  "agent_mode": "SOLO",
  "recording_enabled": true,
  "caller_display_name": "Sam"
}
```

**Errors:** `404 code_not_found`, `410 code_expired`, `409 role_occupied`,
`409 session_not_joinable`.

### `POST /v1/sessions/{id}/token/refresh`

Auth: caller session **or** support user. Returns a fresh LiveKit token for the
same identity. Clients call this at T‑2min. Response is the `livekit` block.

### `GET /v1/sessions/{id}`

Auth: caller session, support user, or service. Full session state.

```json
{
  "session_id": "…", "state": "active", "room_name": "rs_…",
  "ai_enabled": true, "agent_mode": "ASSISTED", "recording_enabled": true,
  "metadata_version": 7,
  "participants": [
    {"role":"caller","identity":"caller-3af91b0c","display_name":"Sam","joined_at":"…","left_at":null},
    {"role":"support","identity":"support-u_412","display_name":"Dana","joined_at":"…","left_at":null},
    {"role":"agent","identity":"agent","joined_at":"…","left_at":null}
  ],
  "recordings": [{"kind":"track_audio","role":"caller","state":"active","gcs_uri":null}],
  "started_at": "…", "ended_at": null
}
```

### `POST /v1/sessions/{id}/end`

Auth: caller session or support user. Deletes the LiveKit room, which cascades
to egress shutdown and agent job termination. Response `202`.

---

## AI agent control

### `POST /v1/sessions/{id}/agent`

Auth: **support user only.** The caller cannot toggle the AI.

**Request**
```json
{ "enabled": false, "reason": "handling directly" }
```

**Response `200`**
```json
{ "ai_enabled": false, "metadata_version": 8, "applied_at": "2026-08-19T18:02:11Z" }
```

Side effects, in order: update `sessions.ai_enabled` → insert `agent_events` row
→ increment `metadata_version` → `RoomService.update_room_metadata`. If the
metadata update fails the DB write is **not** rolled back; the agent reconciles
from `GET /v1/sessions/{id}` on its next heartbeat (30 s).

### `POST /v1/sessions/{id}/agent/mode`

Auth: service (agent worker reporting) or support user (forcing a mode).
Body `{"mode": "WRAP_UP"}`. Writes `agent_events` and syncs room metadata.

---

## Transcripts

### `POST /v1/sessions/{id}/utterances`

Auth: service. Batched, idempotent. Called by the agent worker.

**Request**
```json
{
  "utterances": [
    {
      "client_utterance_id": "01J8…",
      "role": "support",
      "identity": "support-u_412",
      "source": "support_stt",
      "start_ms": 15200,
      "end_ms": 19000,
      "text": "Hi, this is Dana, I've got your details up.",
      "language": "en",
      "confidence": 0.91,
      "agent_mode": "ASSISTED",
      "ai_enabled": true
    }
  ]
}
```

**Response `200`** — `{"accepted": 1, "duplicates": 0}`.
Insert uses `ON CONFLICT (session_id, client_utterance_id) DO NOTHING`.

### `GET /v1/sessions/{id}/transcript`

Auth: caller session, support user, or service.
Query: `?since_ms=0&limit=500&cursor=…`. Returns ordered utterances plus a
`next_cursor`. The support app polls this only as a fallback; the primary live
path is the LiveKit `lk.transcription` text stream (see [06](06-recording-transcripts.md)).

### `POST /v1/sessions/{id}/transcript/export`

Auth: service or support user. Forces the JSONL/VTT/TXT export. Normally
triggered automatically by the `room_finished` webhook; this endpoint exists for
retry. Response `202` with the three target `gs://` URIs.

---

## Recordings

### `GET /v1/sessions/{id}/recordings`

Auth: support user (or caller session if `ALLOW_CALLER_DOWNLOAD=true`).

```json
{
  "recordings": [
    {
      "kind": "track_video", "role": "caller", "state": "complete",
      "mime_type": "video/mp4", "duration_ms": 412000, "size_bytes": 38210496,
      "download_url": "https://storage.googleapis.com/...&X-Goog-Signature=...",
      "url_expires_at": "2026-08-19T18:30:00Z"
    }
  ],
  "transcript": {
    "jsonl_url": "https://…", "vtt_url": "https://…", "txt_url": "https://…"
  }
}
```

### `DELETE /v1/sessions/{id}/data`

Auth: support user with `admin` claim. Purges GCS objects, transcript rows, and
export files; writes a `data_purges` tombstone; sets state `purged`.
Response `200` with counts.

---

## Webhooks

### `POST /v1/webhooks/livekit`

Auth: LiveKit webhook signature (`WebhookReceiver.receive`). **Always return
`200` quickly** — enqueue work rather than doing it inline, and make every
handler idempotent, because LiveKit retries.

| Event | Handler action |
|---|---|
| `room_started` | mark session `active` |
| `participant_joined` | upsert `session_participants`; set `started_at` if null; if support → notify agent of mode change |
| `track_published` | **start Track Egress** for that track (skip agent's own track, skip if `recording_enabled=false`); when both humans have published, start Room Composite Egress |
| `track_unpublished` | no-op (egress ends on its own) |
| `participant_left` | set `left_at`; if a human left → mode `WRAP_UP` |
| `egress_started` | `recordings.state = active` |
| `egress_updated` | mirror state |
| `egress_ended` | set `state`, `gcs_uri`, `duration_ms`, `size_bytes`, or `error` |
| `room_finished` | set `ended_at`, state `completed`, trigger transcript export |

`track_published` is the linchpin of FR-5.4 — it is the only moment a track SID
becomes known, and Track Egress requires one.

---

## Health

`GET /healthz` — liveness. `GET /readyz` — checks Postgres, LiveKit API, GCS.
`GET /metrics` — Prometheus.
