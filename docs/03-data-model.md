# 03 — Data Model

PostgreSQL 15+. SQLAlchemy 2.0 declarative models, Alembic migrations.
All timestamps are `TIMESTAMPTZ`, stored UTC.

## Enums

```sql
CREATE TYPE session_state AS ENUM (
  'pending',            -- created, awaiting consent
  'consent_declined',
  'active',             -- room exists, at least one human connected
  'completed',
  'failed',
  'purged'
);

CREATE TYPE participant_role AS ENUM ('caller', 'support', 'agent');

CREATE TYPE agent_mode AS ENUM ('SOLO', 'ASSISTED', 'WRAP_UP');

CREATE TYPE recording_kind AS ENUM (
  'track_video',
  'track_audio',
  'room_composite'
);

CREATE TYPE recording_state AS ENUM (
  'starting', 'active', 'ending', 'complete', 'failed', 'aborted'
);

CREATE TYPE utterance_source AS ENUM (
  'agent_stt',       -- caller speech, via AgentSession STT
  'support_stt',     -- support speech, via parallel STT stream
  'agent_llm'        -- agent's own generated reply
);
```

## Tables

### `sessions`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | |
| `room_name` | `text` UNIQUE NOT NULL | `rs_{id}` |
| `state` | `session_state` NOT NULL | default `pending` |
| `join_code` | `char(6)` | nullable once expired; see partial unique index |
| `join_code_expires_at` | `timestamptz` | |
| `caller_identity` | `text` | assigned at consent time |
| `support_identity` | `text` | assigned at join time |
| `support_user_id` | `text` | FK to your identity provider's subject |
| `ai_enabled` | `boolean` NOT NULL default `true` | FR-4.4 |
| `agent_mode` | `agent_mode` NOT NULL default `'SOLO'` | |
| `metadata_version` | `integer` NOT NULL default 0 | the `v` field |
| `recording_enabled` | `boolean` NOT NULL default `true` | false if declined w/ fallback |
| `started_at` | `timestamptz` | first human media |
| `ended_at` | `timestamptz` | |
| `transcript_exported_at` | `timestamptz` | |
| `created_at` / `updated_at` | `timestamptz` NOT NULL | |

```sql
-- A join code must be unique only among sessions that can still be joined.
CREATE UNIQUE INDEX ux_sessions_active_join_code
  ON sessions (join_code)
  WHERE state IN ('pending','active') AND join_code IS NOT NULL;

CREATE INDEX ix_sessions_state_created ON sessions (state, created_at DESC);
```

`started_at` is the zero point for every transcript offset. It is set once, on
the first `participant_joined` webhook, and never updated.

### `session_participants`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `session_id` | `uuid` FK → sessions ON DELETE CASCADE | |
| `role` | `participant_role` NOT NULL | |
| `identity` | `text` NOT NULL | |
| `display_name` | `text` | |
| `joined_at` / `left_at` | `timestamptz` | |

```sql
CREATE UNIQUE INDEX ux_participants_session_role
  ON session_participants (session_id, role);   -- enforces FR-1.8
```

### `recordings`

One row per egress. Five rows in the happy path.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `session_id` | `uuid` FK NOT NULL | |
| `egress_id` | `text` UNIQUE NOT NULL | from LiveKit |
| `kind` | `recording_kind` NOT NULL | |
| `role` | `participant_role` | null for `room_composite` |
| `track_sid` | `text` | null for `room_composite` |
| `state` | `recording_state` NOT NULL | |
| `gcs_uri` | `text` | `gs://bucket/...`, set on completion |
| `mime_type` | `text` | |
| `duration_ms` | `bigint` | |
| `size_bytes` | `bigint` | |
| `error` | `text` | |
| `started_at` / `ended_at` | `timestamptz` | |

```sql
CREATE INDEX ix_recordings_session ON recordings (session_id);
```

### `transcript_utterances`

The core transcript table. One row per **finalized** utterance (FR-6.3: interims
are never persisted).

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `session_id` | `uuid` FK NOT NULL | |
| `client_utterance_id` | `text` NOT NULL | agent-generated ULID, for idempotency |
| `role` | `participant_role` NOT NULL | speaker |
| `identity` | `text` NOT NULL | |
| `source` | `utterance_source` NOT NULL | |
| `start_ms` | `bigint` NOT NULL | offset from `sessions.started_at` |
| `end_ms` | `bigint` | |
| `text` | `text` NOT NULL | |
| `language` | `text` | BCP-47, from STT |
| `confidence` | `real` | when the provider supplies it |
| `agent_mode` | `agent_mode` | mode at time of utterance |
| `ai_enabled` | `boolean` | AI state at time of utterance |
| `created_at` | `timestamptz` NOT NULL | |

```sql
CREATE UNIQUE INDEX ux_utterance_idem
  ON transcript_utterances (session_id, client_utterance_id);

CREATE INDEX ix_utterance_session_time
  ON transcript_utterances (session_id, start_ms);
```

The idempotency index is what makes the agent's retry-on-failure loop safe: it
can re-POST a batch after a network blip and use `ON CONFLICT DO NOTHING`.

Recording `agent_mode` and `ai_enabled` **on each utterance** is deliberate. It
means the transcript alone answers "was the AI muted when this was said?" without
a join against the event log, which matters when the export is read in isolation.

### `agent_events`

Audit log for everything the agent does that isn't speech.

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `session_id` | `uuid` FK NOT NULL | |
| `event_type` | `text` NOT NULL | see below |
| `actor` | `text` NOT NULL | `support-u_412`, `system`, `agent` |
| `payload` | `jsonb` NOT NULL default `'{}'` | |
| `occurred_at` | `timestamptz` NOT NULL | |

Event types: `agent_dispatched`, `agent_joined`, `mode_changed`,
`ai_enabled_changed`, `agent_interrupted`, `tool_called`, `agent_error`,
`agent_left`, `support_directive`.

### `consent_events`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial` PK | |
| `session_id` | `uuid` FK NOT NULL | |
| `accepted` | `boolean` NOT NULL | |
| `consent_text_version` | `text` NOT NULL | e.g. `v1.0` |
| `ip_address` | `inet` | |
| `user_agent` | `text` | |
| `occurred_at` | `timestamptz` NOT NULL | |

### `data_purges`

Tombstone left behind by `DELETE /v1/sessions/{id}/data`.

| Column | Type |
|---|---|
| `id` `bigserial` PK, `session_id` `uuid`, `requested_by` `text`, `objects_deleted` `int`, `utterances_deleted` `int`, `occurred_at` `timestamptz` |

Purging sets `sessions.state = 'purged'` and nulls out media/transcript content,
but keeps the session row and this tombstone so audits stay coherent.

## GCS object layout

Single bucket, uniform bucket-level access, no public objects.

```
gs://{GCS_BUCKET}/
  sessions/{session_id}/
    media/
      caller-video-{track_sid}.mp4
      caller-audio-{track_sid}.ogg
      support-video-{track_sid}.mp4
      support-audio-{track_sid}.ogg
      composite.mp4
    transcript/
      transcript.jsonl
      transcript.vtt
      transcript.txt
    session.json          # denormalized metadata snapshot, written at export
```

Audio track egress produces Opus in an OGG container. Video container depends on
the negotiated codec — H.264 → `.mp4`, VP8 → `.ivf`. FR-2.4 forces H.264 so the
`.mp4` path is the only one exercised; the egress webhook handler must still read
the actual filename from `EgressInfo` rather than assuming an extension.

Clients never get bucket credentials. Media is served through
`GET /v1/sessions/{id}/recordings`, which returns V4 signed URLs with a
`SIGNED_URL_TTL_SECONDS` (default 900) lifetime.

## `transcript.jsonl` format

One JSON object per line, ordered by `start_ms`:

```jsonl
{"seq":1,"role":"agent","identity":"agent","source":"agent_llm","start_ms":1200,"end_ms":4300,"text":"Hi, thanks for calling support. What can I help you with?","agent_mode":"SOLO","ai_enabled":true}
{"seq":2,"role":"caller","identity":"caller-3af91b0c","source":"agent_stt","start_ms":5100,"end_ms":9800,"text":"My router keeps dropping the connection.","language":"en","confidence":0.94,"agent_mode":"SOLO","ai_enabled":true}
{"seq":3,"role":"support","identity":"support-u_412","source":"support_stt","start_ms":15200,"end_ms":19000,"text":"Hi, this is Dana, I've got your details up.","language":"en","confidence":0.91,"agent_mode":"ASSISTED","ai_enabled":true}
```

`session.json` carries the header the JSONL omits: session id, room name,
participants, start/end times, mode timeline, AI toggle timeline, and the list of
media objects with their durations.
