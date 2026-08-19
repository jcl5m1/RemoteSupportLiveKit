# 09 — Implementation Plan

Seven phases. Each phase is independently demoable and has explicit acceptance
criteria. Do not start a phase until the previous one's criteria pass — the
dependency order is real, not bureaucratic.

Requirement IDs in brackets reference [01-requirements.md](01-requirements.md).

---

## Phase 0 — Environment (½ day)

### Firebase console setup (not scriptable — do this by hand)

`identitytoolkit.googleapis.com` and `firebase.googleapis.com` are already
enabled on `hermes-458420`. The rest requires the console:

1. console.firebase.google.com → **Add project** → select the existing
   `hermes-458420` GCP project (don't create a new one).
2. **Authentication → Sign-in method → Google → Enable.** Set the support email.
3. **Project settings → Your apps** → register the Android app
   (`applicationId`, plus the debug + release SHA-1 fingerprints) and the iOS
   app (bundle id). Download `google-services.json` and
   `GoogleService-Info.plist` into the Flutter project. **Both files are
   gitignored** — they are config, not secrets, but keeping them out of the
   repo avoids leaking the project layout.
4. Grant the first admin their custom claim once they've signed in:
   ```
   admin.auth().setCustomUserClaims(uid, { role: 'support', admin: true })
   ```
   Until then, `SUPPORT_ADMIN_EMAILS` in `backend/.env` bootstraps it.

SHA-1 fingerprints are the usual stumbling block — Google sign-in fails
silently on Android without them, and the debug and release keystores need
separate entries.


- [ ] LiveKit Cloud project; capture `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
- [ ] GCS bucket with uniform bucket-level access + public access prevention.
- [ ] Service account with `roles/storage.objectAdmin` scoped to the bucket; JSON key into Secret Manager.
- [ ] Postgres instance; `DATABASE_URL`.
- [ ] LiveKit Cloud Inference enabled — covers Deepgram STT, Kimi K2.6 LLM and
      Cartesia TTS under the single `LIVEKIT_API_KEY`. **No per-provider keys.**
      Confirm the three identifiers resolve before phase 2; a wrong or retired
      id fails at first use, not at startup.
- [ ] `docker compose up` brings up Postgres + backend + agent locally.

**Accept:** `curl localhost:8000/readyz` returns all dependencies healthy.

---

## Phase 1 — Two-party call (2–3 days)

The foundation. No AI, no recording.

**Backend**
- [ ] SQLAlchemy models + first Alembic migration for `sessions`, `session_participants`.
- [ ] `services/room_codes.py` — Crockford base32 generation, ambiguity remapping, collision retry against the partial unique index. [FR-1.3]
- [ ] `services/livekit_tokens.py` — per-role grants and signed metadata. [02 § token grants]
- [ ] `POST /v1/sessions`, `POST /v1/sessions/join`, `GET /v1/sessions/{id}`, `POST /v1/sessions/{id}/end`, `POST .../token/refresh`.
- [ ] Caller session JWT issuance + verification dependency.
- [ ] Support auth: Google SSO via Firebase. `core/firebase_auth.py` and
      `deps.py` are **already implemented and tested**; remaining work is the
      Firebase console setup (below) and applying `require_support` /
      `require_admin` to the routes.
- [ ] `POST /v1/webhooks/livekit` with signature verification; handle `room_started`, `participant_joined`, `participant_left`, `room_finished`.
- [ ] Role-occupancy enforcement returning `409 role_occupied`. [FR-1.8]

**Flutter**
- [ ] Project scaffold, Riverpod, Dio API client, freezed models.
- [ ] `RoleSelect`, `CallerStart`, `WaitingRoom` (code + QR), `SupportSignIn` (Google SSO), `SupportJoin` (type + scan), `CallScreen`.
- [ ] `firebase_core` init + `SupportAuth` wiring; attach the Firebase ID token
      as `Authorization: Bearer` on every support-tier call; on 401, retry once
      with `forceRefresh: true`.
- [ ] Deep link handling, cold start and warm. [FR-1.7]
- [ ] `CallController` with room event wiring; H.264 publish options. [FR-2.4]
- [ ] iOS/Android permissions and manifests.
- [ ] Token refresh timer.

**Accept**
1. Two physical devices complete a bidirectional A/V call via a typed 6-char code. [FR-1.1–1.6, FR-2.1–2.3]
2. The same call works via QR scan and via a deep link from Messages.
3. A third device attempting to join as support gets `409`.
4. An expired code returns `410`.
5. Killing WiFi for 10 s and restoring it reconnects without user action. [FR-2.5]
6. Recorded evidence that the video track negotiated H.264.

---

## Phase 2 — AI agent, caller-bound (3–4 days)

**Agent worker**
- [ ] Worker scaffold: `AgentServer`, `@server.rtc_session(agent_name="support-agent")`.
- [ ] Parse `ctx.job.metadata`; fetch session state from the backend.
- [ ] `AgentSession` with `RoomOptions(participant_identity=<caller>)`. [FR-3.2]
- [ ] `prompts.py` — `BASE`, `MODE_PROMPTS`, `CALLER_FACING`, `SUPPORT_FACING`; composition function. [FR-3.6]
- [ ] Mode state machine on participant connect/disconnect; `update_instructions()` on transition. [FR-3.5]
- [ ] Direct-address gate: a pre-LLM boolean check, not a prompt instruction. [05 § direct address]
- [ ] Report mode changes via `POST /v1/sessions/{id}/agent/mode`.

**Backend**
- [ ] `services/dispatch.py` — `create_dispatch(agent_name, room, metadata)` on session activation. [FR-3.1]
- [ ] `agent_events` table + writes.

**Accept**
1. Agent joins within 3 s of the caller connecting and greets in `SOLO`. [NFR-2]
2. Support joins → mode flips to `ASSISTED` → agent goes silent.
3. **Support talks for 60 seconds. The agent never speaks.** [FR-3.4] — the single most important test in this project.
4. In `ASSISTED`, the caller says the wake phrase and the agent responds.
5. Killing the agent worker mid-call does not disconnect the humans.
6. `agent_events` shows the dispatch and every mode transition.

---

## Phase 3 — Dual transcription (2–3 days)

**Agent worker**
- [ ] `support_transcriber.py` — subscribe to the support audio track, own STT instance, `FINAL_TRANSCRIPT` only. [FR-3.3, FR-6.3]
- [ ] Context injection as `[SUPPORT]`-tagged turns.
- [ ] `transcript_sink.py` — ULID stamping, mode/`ai_enabled` stamping, `start_ms` from session start, 2 s / 20-item batching, retry with backoff, shutdown flush.
- [ ] Wire all three sources: `user_input_transcribed`, `conversation_item_added`, support finals.

**Backend**
- [ ] `transcript_utterances` migration with the idempotency index.
- [ ] `POST /v1/sessions/{id}/utterances` (service auth, `ON CONFLICT DO NOTHING`).
- [ ] `GET /v1/sessions/{id}/transcript` with cursor pagination.

**Flutter**
- [ ] `lk.transcription` text stream handler → live caption overlay.
- [ ] Support transcript panel, color-coded by role, auto-scroll + jump-to-live. [FR-6.7]

**Accept**
1. A 3-minute conversation yields rows for all three speakers with correct roles. [FR-6.1, FR-6.2]
2. Zero interim rows in the database. [FR-6.3]
3. Replaying the same batch twice inserts nothing the second time.
4. `start_ms` values align with the audio when checked against the recording.
5. Support's speech appears in the agent's context (verify by asking the agent, in `SOLO` after support leaves, to summarize — it should reference what support said).

---

## Phase 4 — Recording (3–4 days)

**Backend**
- [ ] `services/egress.py` — `start_track_egress` per published human track, keyed idempotently on `(session_id, track_sid)`. [FR-5.4]
- [ ] Room composite egress once both humans have published. [FR-5.2]
- [ ] `recordings` migration; `egress_started`/`updated`/`ended` webhook handlers. [FR-5.5]
- [ ] GCS `DirectFileOutput` + `GCPUpload` wiring; filename convention from [03](03-data-model.md).
- [ ] `GET /v1/sessions/{id}/recordings` with V4 signed URLs.
- [ ] Transcript export on `room_finished`: JSONL, VTT, TXT, `session.json`. [FR-6.6]
- [ ] `POST .../transcript/export` retry endpoint.

**Flutter**
- [ ] REC indicator bound to room metadata. [FR-5.7]

**Accept**
1. A completed session produces exactly 5 media objects in GCS with the documented names. [FR-5.1, FR-5.2]
2. Each audio file contains exactly one speaker.
3. Each video file contains exactly one participant.
4. Killing one egress mid-call leaves the other four intact and the call running. [FR-5.6]
5. `transcript.vtt` loads as a caption track over `composite.mp4` and lines up.
6. Signed URLs work and expire on schedule.

---

## Phase 5 — AI toggle (1–2 days)

**Backend**
- [ ] `POST /v1/sessions/{id}/agent` — support-auth only, DB write + `agent_events` + `metadata_version++` + `update_room_metadata`. [FR-4.4, FR-4.6]

**Agent worker**
- [ ] `room_metadata_changed` listener with the `v`-guard.
- [ ] `data_received` fast path on `rs.agent.control`, with a sender-identity check. [08 § trust model]
- [ ] `apply_ai_enabled`: `interrupt()` → `output.set_audio_enabled(x)` → keep transcription and input enabled. [FR-4.2]
- [ ] Gate `generate_reply()` on the flag. [FR-6.5]
- [ ] 30 s heartbeat reconciliation. [05 § recovery]

**Flutter**
- [ ] Support AI switch with `on` / `off` / `pending` states, dual-path send, echo confirmation, 2 s revert.
- [ ] Caller passive AI indicator. [FR-4.5]

**Accept**
1. Toggling off mid-utterance cuts the agent off within 500 ms. [FR-4.3]
2. While off, caller and support transcripts keep accruing; no `agent` rows appear. [FR-4.2, FR-6.5]
3. Toggling back on restores speech with retained context.
4. Restarting the agent worker while off leaves it off. [FR-4.4]
5. A caller client attempting `POST .../agent` gets `403`.
6. A caller client publishing `rs.agent.control` is ignored by the agent.
7. The caller's indicator updates when support toggles.

---

## Phase 6 — Consent, security, retention (2 days)

- [ ] Consent gate: no LiveKit token from `POST /v1/sessions`; token issued only by `/consent`. [FR-7.1]
- [ ] `consent_events` table and writes with IP, UA, text version. [FR-7.2]
- [ ] `ALLOW_UNRECORDED_FALLBACK` behavior. [FR-7.3]
- [ ] `ConsentSheet` in Flutter, non-dismissible, server-supplied text.
- [ ] Rate limits on every endpoint in [08](08-security-compliance.md) § rate limits.
- [ ] `infra/gcs-lifecycle.json` applied. [FR-7.4]
- [ ] `DELETE /v1/sessions/{id}/data` + `data_purges` tombstone. [FR-7.5]
- [ ] Idle-session sweeper; room `max_duration` and `empty_timeout`.
- [ ] Structured JSON logging with `session_id`; assert no transcript text, tokens, or codes are logged. [NFR-7]

**Accept**
1. A client that never calls `/consent` cannot obtain a LiveKit token by any route.
2. Declining ends the session, or proceeds with zero egresses under the fallback flag.
3. `consent_events` has a row with the exact displayed text version.
4. The lifecycle rule deletes a test object after its horizon.
5. `DELETE .../data` removes all 5 objects, all transcript rows, and all 4 export files, and leaves a tombstone.
6. Rate limits return `429` under load.
7. A `@gmail.com` Google account is rejected with `domain_not_allowed`.
8. A non-admin support operator gets `403` on `DELETE .../data`.
9. Clearing both support allowlists rejects every operator (fails closed).

---

## Phase 7 — Hardening (2–3 days)

- [ ] Backend unit tests: code generation and collisions, token grants per role, role occupancy, webhook idempotency, utterance idempotency, transcript export formats.
- [ ] Agent tests: mode transitions, direct-address gate truth table, support-STT never triggers a reply, toggle application.
- [ ] Integration test with a headless LiveKit client driving a full session.
- [ ] Flutter widget tests for `ConsentSheet`, the AI switch state machine, and code entry normalization.
- [ ] Load test: 25 concurrent sessions; watch agent worker CPU and egress cost.
- [ ] Prometheus metrics + dashboards: session count, agent latency, egress failure rate, transcript lag.
- [ ] Alerts: egress failure rate > 5%, transcript POST failure rate > 1%, agent dispatch failures.
- [ ] Runbook: agent worker down, egress backlog, Postgres failover.

**Accept:** the full suite is green in CI; 25 concurrent sessions hold NFR-1/2/3.

---

## Estimate

| Phase | Days |
|---|---|
| 0 Environment | 0.5 |
| 1 Two-party call | 3 |
| 2 AI agent | 4 |
| 3 Dual transcription | 3 |
| 4 Recording | 4 |
| 5 AI toggle | 2 |
| 6 Consent & security | 2 |
| 7 Hardening | 3 |
| **Total** | **~21 working days** |

Single experienced full-stack engineer with LiveKit familiarity. Add ~40% if
LiveKit is new to the implementer; the agent and egress phases are where the
unfamiliarity cost lands.

## Sequencing notes for a coding agent

- Phases 1 and 2 must be strictly sequential — the agent needs a real caller
  identity to bind to.
- Phase 4 (recording) is independent of phases 2, 3, and 5. If parallelizing
  across agents, that's the clean seam.
- Phase 5 depends on phase 2 only, not on 3 or 4.
- Do not begin phase 3 before phase 2's acceptance test 3 passes. Building the
  transcript pipeline on top of an agent that still answers support means
  rewriting it.
