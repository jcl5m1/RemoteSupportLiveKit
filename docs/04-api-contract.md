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

Support joins by code or session id. Auth: support user (Firebase ID token).
Exactly one of `join_code` / `session_id` must be set.

**Request**
```json
{ "join_code": "K7R2XM", "display_name": "Dana" }
```
```json
{ "session_id": "9f1c8a20-...", "display_name": "Dana" }
```

`session_id` joins are used by authenticated operators following a short
`/join/<session_id>` link; they are not subject to the 6-char join-code TTL.

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

**Errors:** `400 invalid_request` (neither/both lookups set),
`404 code_not_found`, `410 code_expired` (code joins only),
`409 role_occupied`, `409 session_not_joinable`.

### `GET /v1/sessions/public-config`

No auth. Firebase Web SDK config for browser Google sign-in (the API key is
not a secret — it ships in any sign-in client):

```json
{
  "firebase_api_key": "AIza...",
  "firebase_auth_domain": "your-project.firebaseapp.com",
  "firebase_project_id": "your-project"
}
```

### `GET /join/{session_id}`

Short support join link. Redirects (302) to
`/support-web/?session=<session_id>`, where the web client runs the Google
sign-in flow and then calls `POST /v1/sessions/join` with the Firebase ID
token. Knowledge of the session UUID is required, and support auth +
allowlist checks still apply.

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

## Open calls

Plain multi-party A/V rooms joined by link only (no sign-in, consent, or agent). Disabled unless `OPEN_CALLS_ENABLED=true`; all endpoints return `404`
otherwise. Rooms are named `call_<call_id>`, disjoint from session rooms
(`rs_<uuid>`). Call ids are 3–64 chars of `[a-z0-9-]`, lowercased.

### `GET /` — web client entry point
`200` HTML (the join screen; the server injects `window.APP_BASE` /
`window.APP_CALL_ID` and assets load from `/support-web/`). The **query
parameter name identifies the kind of link**, so new support types can share
the entry point:
- `?call=<code>` — open meeting (canonical meeting link, served directly).
- `?session=<id>` — support session (Google sign-in flow); `/join/<id>` redirects here.
- no parameter — new meeting: the client generates an editable `xxx-xxxx-xxx`
  code and offers "Start meeting".

Redirects (`302`) to `/?call=<code>`: non-canonical spellings
(`?call=Team-Sync`), nameless shorthands `?=<code>` and bare `?<code>`, the path
form `/call/<code>`, and `/call?call=<code>`. `/call` alone redirects to `/`.
Legacy `/support-web/?call=<code>` still works; the client rewrites the address
bar to `/?call=<code>`.

### `GET /v1/calls/{call_id}`
`{ "call_id", "recording_enabled", "recording_active", "room_active",
"participant_count", "maps_api_key", "maps_map_id" }` — recording notice before
joining, whether the meeting is currently running, and the browser Maps key the
native app uses for "/show" maps (its web view loads with the public base URL as
origin, so the referrer restriction still applies).

### `POST /v1/calls/{call_id}/join`
Auth: none. Rate limit: `OPEN_CALL_JOIN_RATE_LIMIT`/min/IP (default 120). Body `{ "display_name"?: string }`.
Creates the room if needed (capped at `OPEN_CALL_MAX_PARTICIPANTS`, default 12)
and returns `{ "call_id", "livekit": LiveKitCredentials }` with a fresh
`guest-<name>-xxxxxxxx` identity, plus `recording_enabled`. `400 invalid_call_id`
on a malformed id.

### `POST /v1/calls/{call_id}/end`
Called by the web client when the **last** participant hangs up (the hang-up
button otherwise just leaves). Auth: this call's LiveKit token (any participant). Stops all recording egress
(files finalize), sets room metadata `{"recording": false, "ended_by": <name>}`,
then deletes the LiveKit room, disconnecting everyone (`ROOM_DELETED`). `202`
`{ "ended": true, "ended_by" }`; idempotent if the room is already gone.

### `GET /v1/calls/{call_id}/media-check?url=<url>`
Validates a chat `/show <url>` link before it is sent. Auth: this call's
LiveKit token; 30/min/IP. Follows up to 5 redirects; every hop must be http(s)
and resolve only to public IPs (SSRF guard); only headers are read (HEAD, GET
fallback without reading the body). `200`
`{ "url", "final_url", "kind", "embed_url", "content_type", "status", "geo" }` with
`kind` ∈ `youtube | map | image | audio | video | webpage | link`; `embed_url`
is what receivers load full screen (YouTube/Maps embed URLs; `null` when a page
forbids framing). For maps, `geo` = `{lat, lng, zoom, label}` (from the link's
`@lat,lng,zoomz` / coordinate `q`, else OpenStreetMap Nominatim geocoding; `null`
if not found) drives an interactive Leaflet map; `embed_url` is the fallback. A `429` from the target counts as resolvable. Otherwise `422
media_unavailable` with a user-facing message (e.g. "Link didn't load (HTTP 404)").
The chat message carries the result as `show`; receivers re-validate it is
http(s) before rendering.

### `POST /v1/calls/{call_id}/recording/{start|stop|sync}`
Auth: `Authorization: Bearer <LiveKit token from /join>`; the token's room grant
must be this call's room (`401`/`403` otherwise). `403 recording_disabled` unless
`OPEN_CALL_RECORDING_ENABLED=true`. Returns `{ "recording", "started_by" }`.
Any participant may start or stop. `sync` is a client nudge after publishing a
track so it is recorded immediately (a no-op when recording is off).

### `GET /v1/calls/{call_id}/recordings`
Auth: none — anyone with the call link may view its recordings (product
decision). Rate limit 60/min/IP. Returns `{ "call_id", "recordings": [ { "name",
"participant", "identity", "source", "started_at", "size_bytes", "content_type",
"url" } ] }`, oldest first; `url` is a V4 signed GCS URL valid for
`SIGNED_URL_TTL_SECONDS`. Also `"event_logs": [ { "name", "started_at",
"size_bytes", "url" } ]` — the call's interaction logs (below).

### `POST /v1/calls/{call_id}/photos`
Auth: call token. Raw image body, `Content-Type: image/jpeg | image/png |
image/webp` (`415` otherwise; magic bytes must match, `400 invalid_photo`), ≤25 MB
(`413`); 20/min per identity. Query: `requested_by` (identity of who asked),
`width`, `height`. Stored at `calls/<call_id>/photos/<UTC ms>_<identity>_<hex>.<ext>`
and logged as a `photo` event. Returns `201 { "name", "participant", "identity",
"taken_at", "size_bytes", "content_type", "path", "url" }`; `path` is the stable
link below (relative to the app base), `url` a short-lived signed URL.

### `GET /v1/calls/{call_id}/photos/{filename}`
Auth: none (anyone with the call link, like recordings). `302` to a fresh signed
URL; `404` for names that aren't call photos. The recordings list also returns
`"photos": [...]` (same shape).

**Remote phone camera (data topic `camctl`).** A phone client announces
`{type:"state", phone, cam, cams, torch, torchOn}` (to everyone on change, to
newcomers on join). Others send `{type:"cmd", op:"cycle"|"torch"|"photo", id,
on?}` to the phone only (`destinationIdentities`); it replies `{type:"result", id,
op, ok, error?}`. For `photo`, the phone captures a full-resolution still
(`ImageCapture.takePhoto`, else raises the track to its maximum size for one frame),
uploads it, and sends the requester a `chat` message whose `show` is the image
(`title: "Photo from <name>"`).

### `POST /v1/calls/{call_id}/events`
Auth: call token (as for recording). Body `{ "events": [ { "id", "type", "ts",
"data" } ] (≤100), "sent_at" }`; `ts`/`sent_at` are client epoch ms. Types:
`chat, pointer, mute, media_toggle, camera_switch, device_switch, stage_focus,
show_close, leave, data_packet, camera_control, torch`; `data` ≤8 KB; 120 events/min per identity;
duplicate `id`s are ignored. Returns `202 {"stored"}`. The server time of each
event is `ts + (received − sent_at)` (removes batching delay and client clock
skew; clamped to ±10 min).

**Interaction log.** Events (plus server-side `join`, `recording_start`,
`recording_stop`, `call_end`) are kept in `call_events` and exported as JSONL to
`calls/<call_id>/<UTC>_<room_sid>_events.jsonl`, one line per event:
`{ts, received_ts, client_ts, type, identity, name, data}`, sorted by `ts`
(ISO-8601 UTC, same clock as the recording file names). Re-exported 30 s after
new events and immediately on recording stop / call end.

**Recording.** State lives in LiveKit room metadata
(`{"recording", "started_by", "started_at" | "stopped_by", "skipped"}`) so every
client sees changes via `RoomMetadataChanged`. While on, a backend loop (every
3 s) plus `sync` start one Participant Egress per person who publishes audio or
video: their camera and mic muxed into one MP4 (H.264 + AAC) at
`calls/<call_id>/<UTC>_<identity>.mp4` in `GCS_BUCKET`. A camera turned on later
lands in the same file; late joiners get their own. `stop` stops every live
egress in the room; each start/stop cycle yields new files. One egress per
person (not per track) halves use of the LiveKit project's concurrent-egress
cap (observed 2–5 on the current plan); `skipped` counts people LiveKit refused,
retried every loop tick.

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
