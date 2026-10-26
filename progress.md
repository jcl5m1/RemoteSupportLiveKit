# RemoteSupportLiveKit — Implementation Progress

> Living document. Completed steps, open issues, blockers, and questions for async review.

## Project status snapshot

- **Docs read:** all 10 spec docs (`00-overview` through `10-risks-references`) and the implementation plan.
- **Goal:** implement the full spec with tests as described in `docs/09-implementation-plan.md`.
- **Current test status:**
  - Backend: **44/44 passing**, `ruff` clean, `mypy` clean.
  - Agent: **17/17 passing**, `ruff` clean.
  - Flutter: **11/11 passing**, `flutter analyze` clean.
- **Stale TODOs:** Resolved the last tracked source-code TODO (`mobile/lib/models/call_state.dart`). The `@freezed` codegen is intentionally deferred; a hand-written `CallState.copyWith` extension is used instead.
- **CI:** Added `.github/workflows/ci.yml` running backend, agent, and Flutter checks on every push/PR to `main`.
- **Observability:** Added `remote_support_transcript_batches_total` counter, `remote_support_readyz_dependency_healthy` gauge, example Prometheus alert rules in `infra/monitoring/alerts.yml`, and a Grafana dashboard JSON in `infra/monitoring/dashboard.json`.
- **Local dev helpers:** Added `scripts/test-all.sh` (runs all tests and lints) and `scripts/dev-start.sh` (starts Postgres + backend + agent without Docker). The agent worker now exports LiveKit connection params from `agent/.env` to the `livekit.agents` CLI so the same `.env` file works for both settings and CLI startup.
- **API contract:** Added `scripts/export-openapi.py` and committed `docs/openapi.json`; CI includes an OpenAPI drift check so the spec stays in sync with the FastAPI code.
- **Contributor workflow:** Added `.pre-commit-config.yaml` with hooks for backend ruff/mypy/tests, agent ruff/tests, Flutter analyze/tests, and OpenAPI drift check.
- **Rate limiting:** Made utterance ingest rate limit configurable (`UTTERANCE_RATE_LIMIT`, `UTTERANCE_RATE_LIMIT_WINDOW_SECONDS`) and fixed the transcript metric test to be deterministic across rate-limit windows.
- **Load testing:** Added `scripts/load-test.py` for backend-only load testing (session creation, consent, utterance ingest) in degraded mode. This is NOT a full LiveKit/media load test; that still requires a headless LiveKit client.
- **Documentation:** Updated `README.md` with current test counts, `scripts/dev-start.sh`, `scripts/test-all.sh`, pre-commit hooks, and degraded-mode development flow.
- **Bootstrap:** Added `scripts/setup.sh` to verify prerequisites and create backend/agent venvs on a fresh clone.
- **Web support client:** Added `backend/app/static/support-web/` (HTML + JS using LiveKit JS SDK from CDN). Joins a room via `?token=&ws_url=`, publishes camera/mic, subscribes to remote tracks, and exposes `window.testState` flags for Playwright.
- **Internal test endpoint:** Added `POST /internal/test/support-token` in `backend/app/routers/testing.py`, gated by `ALLOW_TEST_ENDPOINTS=true` and `X-Service-Key`. It loads the session, starts the LiveKit room if pending, creates the support participant row, and mints a support LiveKit token with a synthetic identity (`support-test-support`).
- **Conditional static mount:** `backend/app/main.py` now includes the testing router and mounts `/support-web/` only when `settings.allow_test_endpoints` is true.
- **Playwright regression harness:** Added `tests/e2e/` with `requirements.txt`, `conftest.py`, `.env.example`, and `test_two_web_clients.py`. The test creates a caller session, records consent, calls the internal support-token endpoint, opens two headless Chromium tabs, asserts both sides connect and see remote video/audio, and verifies inbound media bytes increase.
- **Local E2E validation:** The two-web-client Playwright test passes against a local backend with real LiveKit Cloud credentials (headless Chromium with fake A/V devices).
- **Production E2E validation:** After gcloud re-authentication, the new backend image was built, pushed to Artifact Registry, loaded on the GCP VM, and the production container was restarted. `GET https://remotesupport.lgitech.net/support-web/` returns 200 and the Playwright two-web-client test passes against `https://remotesupport.lgitech.net`.
- **CI workflow:** Added `.github/workflows/e2e.yml` to run the two-web-client test nightly and on workflow dispatch against the live backend using repository secrets (`E2E_BACKEND_URL`, `E2E_SERVICE_API_KEY`, `E2E_LIVEKIT_URL`).
- **Documentation:** Updated `docs/02-architecture.md` with the web support client and Playwright regression harness. Added `docs/12-user-guide.md` covering caller/support flows, permissions, and troubleshooting. Rewrote `README.md` with an architecture diagram, quick-start usage, expanded local/prod test instructions, and production deployment notes.
- **Cloud credentials:** Verified LiveKit and GCS credentials in `backend/.env` and `agent/.env`. Fixed GCS client initialization to use `GCP_CREDENTIALS_B64`; fixed `/readyz` GCS check to use `list_blobs` instead of `bucket.exists()`. `GET /readyz` now reports all dependencies healthy.
- **Integration smoke test:** Added `scripts/integration-smoke-test.py` which starts the backend, creates a session, records consent, verifies the LiveKit room exists, ends the session, and cleans up. It passes with the real cloud credentials.
- **Agent worker registration:** Verified the agent worker starts and registers with LiveKit Cloud (`registered worker ... id: AW_xFAxwtTGJJCL`).
- **Container runtime:** Installed Colima + Docker CLI + Docker Compose via Homebrew. Validated `docker compose up --build -d` with postgres + backend + agent; `/readyz` healthy; integration smoke test passes against containerized backend.
- **Mobile config checker:** Added `scripts/check-mobile-config.py` to validate Android/iOS bundle ids, universal-link domains, entitlements, and Firebase config file presence.
- **Android production config:** Found `mobile/google-services.json` with production bundle id `net.lgitech.remotesupport`; moved it to `mobile/android/app/google-services.json`, applied the bundle id to Android/iOS manifests, and added the `com.google.gms.google-services` plugin to the Android build. Added `mobile/android/key.properties.example` and updated `mobile/android/app/build.gradle` to load release signing from `key.properties` when present, falling back to debug signing otherwise. Moved `MainActivity.kt` from `com.example.remote_support` to `net.lgitech.remotesupport` so the manifest resolves the activity correctly. Android release APK builds successfully (~107 MB) with the production bundle id.
- **Integration tests:** Added `scripts/integration-caller-flows.py` covering create → consent → end → room deleted, consent idempotency, and declined consent without unrecorded fallback. All pass against a backend with real LiveKit/GCS credentials.
- **Remaining mobile blockers:** universal-link domain is still `support.example.com` (no evidence of real value); `mobile/ios/Runner/GoogleService-Info.plist` is missing.
- **Mobile config helper:** Added `scripts/apply-mobile-config.py` to apply production bundle id, universal-link domain, and iOS app group to Android/iOS manifests in one command. Added `--android-only` so iOS files can be skipped while the iOS app is on hold.
- **GCP deployment:** Backend + agent + Postgres + Caddy are running on a Compute Engine VM (`remote-support-vm`, `us-central1-a`, `e2-medium`) in project `hermes-458420`. The agent has registered with LiveKit Cloud (`AW_DmG5kG8iYJuL`). HTTP health checks pass via the VM's external IP. HTTPS / custom domain is waiting on DNS.
- **Current status:** Android backend is live; iOS remains on hold. The remaining blocker is the DNS `A` record for `remotesupport.lgitech.net` and the release signing cert SHA-256 for `assetlinks.json`.

## Environment blockers

### Python 3.12 not available locally — RESOLVED
- Installed Python 3.12.14 via Homebrew; backend and agent venvs created with pinned deps.

### PostgreSQL not available locally — RESOLVED
- Installed `postgresql@16` via Homebrew; created `rs` user / `remote_support` and `remote_support_test` databases.

### No cloud credentials — RESOLVED
- **Discovery:** `backend/.env` and `agent/.env` contain LiveKit Cloud and GCS service-account credentials.
- **Verification:** LiveKit `ListRooms` succeeds; GCS object listing succeeds for the configured bucket.
- **Bug fixed:** `backend/app/main.py` was initializing `gcs.Client()` with default credentials instead of the service account from `GCP_CREDENTIALS_B64`. It now uses `gcs.Client.from_service_account_info(...)` when the base64 credential is present.
- **Bug fixed:** `/readyz` used `bucket.exists()`, which requires `storage.buckets.get` permission that the egress service account lacks. It now uses `list_blobs(max_results=1)`, which only needs object-list permission.
- **Result:** `GET /readyz` now reports `healthy` with Postgres, LiveKit, and GCS all `ok`.

### Android SDK + JDK not available locally — RESOLVED
- Installed JDK 21.0.12+8 under `.jdk/`.
- Installed Android SDK command-line tools under `.android-sdk/`; accepted licenses.
- `sdkmanager --list_installed` shows platform-tools 37.0.1, platforms/android-35, build-tools/35.0.0.
- Gradle auto-downloaded CMake 3.22.1, Android SDK Build-Tools 36, and platforms/android-36 during the first build.

### Flutter SDK not installed locally — RESOLVED
- Installed Flutter 3.47.0 (Dart 3.13.0) under `.flutter-sdk/`.
- `flutter pub get` resolves against `pubspec.yaml` (livekit_client 2.11.0).
- `flutter analyze` reports no issues.
- `flutter test` passes all 11 tests.
- Android release APK builds successfully with the updated Gradle/AGP/Kotlin stack (see below).

### iOS build host — BLOCKED
- Xcode command-line tools are installed (`/Library/Developer/CommandLineTools`), but `flutter build ios` requires the full Xcode IDE.
- `xcodebuild -version` fails with: "tool 'xcodebuild' requires Xcode, but active developer directory ... is a command line tools instance".
- iOS build validation and entitlements signing cannot proceed without full Xcode.

### Docker Compose local stack — RESOLVED
- `infra/docker-compose.yml` defines `postgres`, `backend`, and `agent` services.
- Removed hardcoded `ALLOW_DEGRADED_START=true` from `infra/docker-compose.yml`; the value is now read from `backend/.env` / `agent/.env`, so real credentials are used when present.
- Changed Docker build contexts from project root to `backend/` and `agent/` and updated `infra/Dockerfile.backend` / `infra/Dockerfile.agent` accordingly, shrinking build contexts from >1 GB to ~70 MB.
- Added `alembic upgrade head` to the backend startup command so the container creates tables on first run.
- In degraded mode `/readyz` reports cloud dependencies unhealthy while Postgres is healthy; the agent container idles instead of crashing.
- The backend still requires `CALLER_JWT_SECRET`, `SERVICE_API_KEY`, and `FIREBASE_PROJECT_ID` to start (see `backend/.env.example`).
- `/readyz` and `/healthz` endpoints are already implemented in `backend/app/main.py`.
- Added `scripts/dev-start.sh` to start Postgres + backend + agent directly without a container runtime.
- **Container runtime installed:** Colima + Docker CLI + Docker Compose via Homebrew; Colima VM is running.
- **Validation:** `docker compose up --build -d` starts postgres, backend, and agent; `GET /readyz` reports `healthy` (postgres, livekit, gcs all `ok`); `scripts/integration-smoke-test.py --external-backend` passes end-to-end against the containerized backend.

### Google Cloud production deployment — RESOLVED
- **VM:** `remote-support-vm` in `us-central1-a`, machine type `e2-medium`, external IP `35.253.224.64`.
- **Firewall:** created `default-allow-http` and `default-allow-https` ingress rules for tags `http-server`/`https-server`.
- **Images:** Built `backend` and `agent` images directly on the VM (avoids cross-platform issues from Apple Silicon) and tagged them in `us-central1-docker.pkg.dev/hermes-458420/remote-support/`.
- **Registry:** Created Artifact Registry repository `remote-support` in `us-central1` (images pushed earlier; VM build now takes precedence).
- **Compose:** Added `infra/docker-compose.prod.yml` (postgres + backend + agent + Caddy), `infra/Caddyfile.prod`, and `infra/assetlinks.json`.
- **TLS:** Caddy obtained a Let's Encrypt certificate for `remotesupport.lgitech.net`; HTTP requests redirect to HTTPS.
- **Release signing:** Created `mobile/android/remotesupport-release.keystore`, generated `mobile/android/key.properties`, added both to `mobile/.gitignore`, and verified the APK signature SHA-256 matches `infra/assetlinks.json` (`91:24:...:78:94`).
- **Status:**
  - `GET https://remotesupport.lgitech.net/healthz` → `{"status":"ok"}`
  - `GET https://remotesupport.lgitech.net/readyz` → healthy (postgres, livekit, gcs all `ok`)
  - `GET https://remotesupport.lgitech.net/.well-known/assetlinks.json` served correctly
  - Google Digital Asset Links API confirms the statement for `https://remotesupport.lgitech.net` → `net.lgitech.remotesupport`
  - Agent logs show `registered worker` against LiveKit Cloud
  - API session creation returns universal links using `https://remotesupport.lgitech.net/j/{code}`

## Implementation plan tracker

### Phase 0 — Environment
- [x] Python 3.12 runtime available.
- [x] Backend dependencies installed.
- [x] Agent dependencies installed.
- [x] Postgres running locally.
- [x] LiveKit Cloud project + credentials captured.
- [x] GCS bucket + service-account key captured.
- [x] Backend and agent can start without LiveKit/GCS credentials via `ALLOW_DEGRADED_START`.
- [x] `docker compose up` exercised end-to-end with postgres + backend + agent; `/readyz` healthy and integration smoke test passes.
- [x] `/healthz` and `/readyz` endpoints implemented.
- [x] `GET /readyz` returns all dependencies healthy.

### Phase 1 — Two-party call

#### Backend
- [x] SQLAlchemy async engine + session factory.
- [x] Alembic migration for all 8 tables and Postgres enums.
- [x] `services/room_codes.py` with generation, normalization, and `claim_code`.
- [x] `services/livekit_tokens.py` with per-role grants and signed metadata.
- [x] Auth dependencies: caller JWT, support IdP JWT, service key, admin claim.
- [x] Session lifecycle endpoints in `routers/sessions.py`.
- [x] LiveKit webhook receiver skeleton in `routers/webhooks.py`.
- [x] Agent control router (`routers/agent.py`).
- [x] Transcript ingest/read/export router + service.
- [x] Egress service (`services/egress.py`) and recordings router.
- [x] Role-occupancy enforcement returning `409 role_occupied`.
- [x] HTTP exception handler returning the contract error shape `{ "error": { "code", "message", "details" } }`.
- [x] Backend unit tests green: `test_room_codes.py`, `test_livekit_tokens.py`, `test_sessions.py`, `test_support_auth.py`, `test_rate_limits.py` (35/35 passing).

#### Flutter
- [x] Riverpod providers and `CallController` wiring (`providers.dart`, `services/call_controller.dart`).
- [x] Implement screens: `RoleSelect`, `CallerStart`, `ConsentSheet`, `WaitingRoom`, `SupportJoin`, `SupportSignIn`, `CallScreen`, `CallSummary`.
- [x] Deep link handling (`app_links`) for cold start and warm links.
- [x] Token refresh timer (`CallController.scheduleTokenRefresh`) — stores refreshed token via `prepareConnection`; live `setToken` not exposed by livekit_client 2.x.
- [x] iOS/Android permissions and manifests (`Info.plist`, `Runner.entitlements`, `AndroidManifest.xml`, `app/build.gradle` minSdk 24).
- [x] Android release build validated: `flutter build apk --release` produces `build/app/outputs/flutter-apk/app-release.apk` (106.9 MB) after updating AGP to `9.0.1`, Kotlin to `2.2.20`, and Gradle wrapper to `9.1.0`.
- [x] Added `scripts/apply-mobile-config.py` to patch Android/iOS bundle id, universal-link domain, and iOS app group from a single command.

#### Acceptance
- [x] Backend can create a real LiveKit room on consent and delete it on session end (verified via `scripts/integration-smoke-test.py`).
- [x] Android release APK builds with production bundle id and Firebase config.
- [ ] Two devices complete a bidirectional A/V call via typed 6-char code (blocked: real devices).
- [ ] QR scan and deep link join both work (blocked: real devices / universal-link domain).
- [ ] Third support device gets `409`.
- [ ] Expired code returns `410`.
- [ ] 10 s WiFi drop recovers.
- [ ] H.264 negotiated.

### Phase 2 — AI agent, caller-bound
- [x] `SessionState.load` fetches backend state and replays transcript context.
- [x] `SupportAgent.on_user_turn_completed` direct-address gate.
- [x] Mode state machine (`SOLO` → `ASSISTED` → `WRAP_UP`) with `update_instructions()`.
- [x] `POST /v1/sessions/{id}/agent/mode` integration from worker.
- [x] `agent_events` writes observed from worker (via backend webhooks).
- [x] Agent tests: mode transitions, direct-address gate truth table.
- [x] Extended direct-address gate to catch second-person questions naming the assistant at the end (e.g. "can you look that up, assistant?").

### Phase 3 — Dual transcription
- [x] `transcript_utterances` migration with idempotency index (already in schema).
- [x] `POST /v1/sessions/{id}/utterances` idempotent ingest.
- [x] `GET /v1/sessions/{id}/transcript` cursor pagination.
- [x] `SupportTranscriber` implementation.
- [x] `TranscriptSink` batching + retry + shutdown flush.
- [x] Caller and agent transcript source wiring in `agent/main.py`.
- [x] Flutter transcript panel + live captions (`widgets/transcript_panel.dart`, `widgets/caption_overlay.dart`).
- [x] Transcript batch outcome metric (`remote_support_transcript_batches_total`) and tests.

### Phase 4 — Recording
- [x] `services/egress.py`: `start_track_egress`, `start_room_composite_egress`, `on_egress_ended`.
- [x] Webhook handlers: `egress_started`, `egress_updated`, `egress_ended`.
- [x] GCS signed URLs in `GET /v1/sessions/{id}/recordings`.
- [x] Transcript export (JSONL, VTT, TXT, `session.json`).
- [x] `services/storage.py` implementation.

### Phase 5 — AI toggle
- [x] `POST /v1/sessions/{id}/agent` support-auth endpoint.
- [x] Agent `room_metadata_changed` + `data_received` listeners.
- [x] `apply_ai_enabled`: interrupt + mute speech, keep transcribing.
- [x] 30 s heartbeat reconciliation.
- [x] Flutter AI switch + caller indicator (`widgets/ai_toggle.dart`, `CallScreen` status bar).

### Phase 6 — Consent, security, retention
- [x] Consent gate enforced: no LiveKit token without `/consent`.
- [x] `consent_events` table and writes.
- [x] `ALLOW_UNRECORDED_FALLBACK` behavior.
- [x] Flutter `ConsentSheet` (`screens/consent_sheet.dart`, server-supplied text, non-dismissible).
- [x] Postgres-backed rate limits on every endpoint from docs/08.
- [x] `DELETE /v1/sessions/{id}/data` + `data_purges` tombstone.
- [x] Idle-session sweeper.
- [x] Structured JSON logging with `structlog`; no transcript/tokens/codes logged.

### Phase 7 — Hardening
- [x] Backend unit tests: code generation, token grants, role occupancy, rate limits, webhook/egress skeleton.
- [x] Agent tests: direct-address gate truth table, AI toggle gate.
- [x] Integration smoke test with real LiveKit/GCS (`scripts/integration-smoke-test.py`).
- [x] Caller-only backend integration flows (`scripts/integration-caller-flows.py`).
- [x] Full integration test with headless LiveKit client joining a room — implemented as two-web-client Playwright harness (`tests/e2e/test_two_web_clients.py`) and passes locally and in production.
- [x] Production GCP deployment of the new backend image — deployed to `remote-support-vm`; `/support-web/` and `/internal/test/support-token` are live on `https://remotesupport.lgitech.net`.
- [x] Flutter widget tests written (`test/utils/join_code_test.dart`, `test/widgets/ai_toggle_test.dart`, `test/screens/consent_sheet_test.dart`) — runnable once Flutter SDK is available.
- [x] Backend-only load test script (`scripts/load-test.py`) for HTTP endpoints.
- [ ] Full load test 25 concurrent LiveKit sessions (blocked: no LiveKit/GCS credentials).
- [x] Prometheus metrics + `/metrics` endpoint.
- [x] GitHub Actions CI workflow for backend, agent, and Flutter checks.
- [x] `/readyz` dependency health Prometheus gauge + alert rules in `infra/monitoring/alerts.yml`.
- [x] Grafana dashboard JSON in `infra/monitoring/dashboard.json`.
- [x] Runbooks: agent worker down, egress backlog, Postgres failover.

## Assumptions & decisions log

1. **Python 3.12 target retained.** Code targets Python 3.12 and the pinned package versions.
2. **No credentials available locally.** Implementation uses `pydantic-settings`; tests use fakes.
3. **Support auth uses Firebase/Google ID tokens.** Backend validates RS256 tokens against Google's JWKS with project-derived issuer/audience and configurable email/domain allowlists.
4. **PostgreSQL enums created idempotently.** The Alembic migration uses a PL/pgSQL block to create enums only if they do not exist.
5. **SQLAlchemy enum labels use member *values*, not names.** Added `values_callable=lambda e: [m.value for m in e]` to every `Enum` column so Postgres labels match the migration and the partial index.
6. **LiveKit AccessToken serializes video grants in camelCase.** Updated token tests to assert `roomJoin`, `roomAdmin`, `canUpdateOwnMetadata` instead of snake_case.
7. **Backend tests use an async HTTP client on the same event loop as the DB fixtures.** Replaced `fastapi.testclient.TestClient` with `httpx.AsyncClient(..., transport=ASGITransport(app=app))` to avoid asyncpg loop mismatch.
8. **HTTP errors return the contract shape.** Added a global `HTTPException` handler so callers see `{ "error": { "code", "message", "details" } }`.
9. **Rate limiting uses Postgres counters.** A `rate_limits` table was added with a composite PK `(key, window_start)`. Counters are inserted/updated atomically with `ON CONFLICT`.
10. **Agent event wiring assumptions.** Caller STT uses `session.on("user_input_transcribed")`; agent LLM uses `session.on("conversation_item_added")` with `role == "assistant"`; support STT uses a separate `livekit.agents.inference.STT` instance. These match the pinned `livekit-agents==1.6.10` source.
11. **Prometheus metrics are process-local counters.** No external push gateway; scrape `/metrics` from the backend pod.
12. **Flutter SDK installed locally for validation.** Flutter 3.47.0 (Dart 3.13.0) is installed under `.flutter-sdk/`, added to `.gitignore`, and used to run `flutter analyze` and `flutter test`. Real device builds still need Android SDK / Xcode.
13. **Caller device id is generated in memory.** In production it should be persisted in secure storage and rotated on user request; for v1 a random 32-char string is generated per app launch.
14. **Flutter state management uses hand-written `CallState`.** The spec shows a `@freezed` model; a hand-written mutable-copy extension (`CallState.copyWith`) is in `services/call_controller.dart`. `freezed`/`build_runner` remain in `pubspec.yaml` for future codegen.
15. **Support join code normalization is centralized in `utils/join_code.dart`.** Ambiguous characters (`I L O U`) are remapped on entry rather than rejected (FR-1.3).
16. **AI toggle uses room-metadata version guard and a 2-second revert timer.** The fast path (`rs.agent.control` data message) and authoritative REST path are both sent from `CallController.setAiEnabled`.
17. **Token refresh uses `Room.prepareConnection` with the new token.** The LiveKit Flutter client 2.x does not expose a public `setToken` on a connected room; the SDK handles server-sent refresh internally and `prepareConnection` caches the token for reconnect.
18. **Support auth lazily accesses `FirebaseAuth.instance`.** The constructor no longer eagerly touches Firebase, so widget tests can inject `FakeSupportAuth` without initializing Firebase.
19. **Platform manifests use placeholder bundle id and universal-link domain.** `com.example.remote_support` and `support.example.com` must be replaced with the production values before release.
20. **Android build toolchain versions.** Validated release build with Flutter 3.47.0, JDK 21, Android SDK 35/36, Gradle 9.1.0, AGP 9.0.1, Kotlin 2.2.20. Flutter prints deprecation warnings about KGP because several plugins still apply the Kotlin Gradle Plugin; the build still succeeds.
21. **Firebase initialization already wired.** `lib/main.dart` calls `Firebase.initializeApp()` before `runApp`; only the platform-specific config files are missing.
22. **iOS build requires full Xcode.** Command-line tools alone are insufficient for `flutter build ios`; the entitlements and signing validation are blocked until full Xcode is available.
23. **Backend and agent startup support a degraded mode.** `ALLOW_DEGRADED_START=true` lets the backend boot with stub LiveKit/GCS clients and lets the agent worker idle without connecting to LiveKit when credentials are missing. `/readyz` reports cloud dependencies unhealthy while Postgres can still be healthy. This is intended for local development only.
24. **Agent CLI reads LiveKit connection params from environment variables.** `agent/agent/main.py` now exports `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` from pydantic settings to the environment before calling `livekit.agents.cli.run_app`, so a single `agent/.env` file works for both settings and CLI startup.
25. **Prometheus alert rules are example YAML only.** `infra/monitoring/alerts.yml` assumes a standard Prometheus scrape config with `job="remote-support-backend"`. Alerts rely on metrics that exist in `backend/app/metrics.py`; a real monitoring stack (Prometheus + Alertmanager) is still required to evaluate and route them.
26. **Utterance ingest rate limit is configurable.** `UTTERANCE_RATE_LIMIT` and `UTTERANCE_RATE_LIMIT_WINDOW_SECONDS` default to 600/minute and can be tuned per environment.
27. **Load test script is backend-only.** `scripts/load-test.py` exercises HTTP endpoints (session creation, consent, utterance ingest) without real LiveKit/GCS. A full 25-session media load test requires a headless LiveKit client.
28. **Mobile config helper normalizes iOS bundle id to match Android.** `scripts/apply-mobile-config.py` replaces both platform placeholders with the same `--bundle-id`. iOS test target keeps the `.RunnerTests` suffix, and an iOS app group is added only when `--app-group` is supplied.
29. **Cloud credentials are present in local `.env` files.** `backend/.env` and `agent/.env` contain LiveKit Cloud and GCS service-account credentials. The backend now uses those credentials explicitly; `/readyz` verifies connectivity with `list_blobs(max_results=1)` rather than `bucket.exists()` because the egress service account has object-level but not bucket-metadata access.
30. **Docker build contexts are scoped to service directories.** `infra/Dockerfile.backend` and `infra/Dockerfile.agent` now expect `backend/` and `agent/` as their respective build contexts, avoiding multi-gigabyte context transfers.
31. **Mobile release config is intentionally left as placeholders.** `com.example.remote_support` and `support.example.com` are used until production values are supplied; `scripts/apply-mobile-config.py` and `scripts/check-mobile-config.py` automate the switch and validation.
32. **Android release signing uses `key.properties`.** `mobile/android/app/build.gradle` reads `mobile/android/key.properties` when it exists; otherwise it falls back to the debug keystore so CI/fresh clones still build. A `key.properties.example` template is committed; the real keystore and passwords are secrets and must not be committed.
33. **Web support client uses the LiveKit JS SDK UMD build from jsDelivr.** The UMD global is `window.LivekitClient` (lowercase "k"), confirmed by inspecting the CDN bundle.
34. **Web client URL parameters.** The page requires both `?token=` (LiveKit JWT) and `?ws_url=` (websocket URL) because the SDK does not extract the URL from the token. The Playwright harness passes both.
35. **Test support identity is synthetic.** `/internal/test/support-token` uses `support-test-support` as the support user id. It is meant for one test run per session; a second call returns `409 role_occupied` if the support slot is already filled.
36. **Headless Chromium fake media.** The Playwright harness launches Chromium with `--use-fake-device-for-media-stream` and `--use-fake-ui-for-media-stream` so no physical camera/mic are needed. Video bytes flow; audio bytes may be silence but still increase once stats are populated.
37. **LiveKit JS SDK peer-connection structure.** In the pinned `livekit-client@2.x`, the active peer connection is reached via `room.engine.pcManager.publisher._pc` (the wrapper exposes `_pc`). Receiving tracks are enumerated via `pc.getStats()` rather than `pc.getReceivers()` because `getReceivers()` returns wrappers, not raw receivers in this build.
38. **LiveKit Cloud Inference TTS is currently unusable in this project.** Every provider/voice tried emitted `no audio frames were pushed`. The agent therefore defaults to `USE_DUMMY_TTS=true`, which uses a local sine-wave `ToneTTS` so the speech scheduler can advance and text replies still flow to the transcript sink. Real speech audio is not produced; set `USE_DUMMY_TTS=false` to re-test cloud TTS once the issue is resolved.
39. **Caller speech in the headless web client is repeated once with a silence gap.** The 2.3-second `caller_prompt.wav` clip is played twice with an 8-second silence gap so the agent's STT has a fresh utterance to transcribe even if it joins after the first play has already started.
40. **Recording egress tests require a public backend URL.** LiveKit Cloud sends `track_published`, `egress_ended`, and `room_finished` webhooks to the backend. When the backend is `localhost`, those webhooks cannot be delivered, so egress never starts and transcript export is not triggered. The E2E recordings test is skipped locally and must run against the deployed backend.

## Open questions

1. ~~Do you have LiveKit Cloud / GCS credentials to add to `backend/.env` and `agent/.env`, or should I leave placeholders and rely on mock-based tests?~~ Resolved: credentials are present and `/readyz` is healthy.
2. Should support sign-in validate the `email_verified` claim in the Firebase ID token, or trust the token issuer?
3. For rate limiting: the current Postgres counter is adequate for the documented scale. Do you want Redis added for a token bucket?
4. ~~**Universal-link domain.**~~ Resolved: using `remotesupport.lgitech.net`. The GCP backend is deployed and the Android manifest is patched.
5. ~~**DNS A record.**~~ Resolved: `remotesupport.lgitech.net` resolves to `35.253.224.64`, HTTPS is active, and Android App Links verify.
6. ~~**Android signing cert SHA-256.**~~ Resolved: release keystore created, fingerprint published in `/.well-known/assetlinks.json`, and verified against the APK.
7. **iOS Firebase config.** `mobile/ios/Runner/GoogleService-Info.plist` is missing. Drop it from the Firebase Console (project `hermes-458420`, bundle id `net.lgitech.remotesupport`).
8. **iOS/macOS build host.** Full Xcode is required to validate the iOS build and entitlements. Should I install full Xcode, or will you validate on a Mac with Xcode?
9. ~~**Docker Compose runtime.**~~ Resolved: Colima + Docker installed and validated locally; GCP production VM also running Docker Compose.
10. ~~**GCP deployment of the new backend image.**~~ Resolved: `gcloud auth login` was refreshed by the user. The backend image was built locally (linux/amd64), pushed to Artifact Registry with the user's credentials, saved to a tar, copied to the VM, loaded with `docker load`, and the production container was restarted. The VM's default compute service account still lacks Artifact Registry pull/push permissions, so future deploys should use the same tar-load workflow or grant that SA `roles/artifactregistry.reader` + `roles/artifactregistry.writer`.

## Next actions

1. ~~Provide the production universal-link domain, then run `scripts/apply-mobile-config.py --bundle-id net.lgitech.remotesupport --app-group group.net.lgitech.remotesupport --universal-link-domain <domain>` to update Android/iOS manifests.~~ Done: using `remotesupport.lgitech.net`.
2. Drop Firebase config files (`google-services.json`, `GoogleService-Info.plist`) into `mobile/android/app/` and `mobile/ios/Runner/`; `Firebase.initializeApp()` is already wired in `lib/main.dart`.
3. ~~Provide LiveKit Cloud / GCS credentials for full `docker compose up` and `/readyz` validation.~~ Credentials verified; `/readyz` healthy.
4. ~~Install a container runtime (Docker Desktop) or validate `docker compose up` on a host with Docker.~~ Colima + Docker installed and `docker compose up` validated.
5. Install full Xcode or validate the iOS build and entitlements on a Mac with Xcode.
6. ~~Run the Android app on a real device to verify A/V, deep links, and permissions.~~ APK installed on Pixel 7; deep links verified. Full A/V call requires a second device/support user.
7. ~~Add end-to-end/integration tests once LiveKit / GCS credentials are available.~~ Done: two-web-client Playwright harness passes locally and in production.
8. ~~**Deploy the new backend image to the GCP VM** so `/support-web/` and `/internal/test/support-token` are available for production regression runs.~~ Done: deployed and verified.
9. ~~**Run the Playwright test against `https://remotesupport.lgitech.net`** once the deployed backend has `ALLOW_TEST_ENDPOINTS=true`.~~ Done: test passed.
10. Add the Android + web regression variant (ADB deep-link launch + web support client).
11. Consider granting the VM's compute service account `roles/artifactregistry.reader` so `docker compose pull` works without the tar-load workaround.
12. Add the required GitHub repository secrets (`E2E_BACKEND_URL`, `E2E_SERVICE_API_KEY`, `E2E_LIVEKIT_URL`) to enable the nightly E2E workflow.
13. Set up alerting rules and dashboards once a monitoring stack is chosen.
14. Investigate and resolve the LiveKit Cloud Inference TTS `no audio frames were pushed` failure so `USE_DUMMY_TTS` can default back to `false`.
15. Run `test_recordings_and_transcript_after_session_end` against the deployed backend (`https://remotesupport.lgitech.net`) to verify egress and transcript export end-to-end.
16. Add lifecycle-specific E2E assertions for agent reconnection after caller network drop and after support joins mid-call.

## Latest turn summary

### Meet-style browser call UI + reconnect robustness fixes

- **Goal:** make the `support-web` in-call experience feel like Google Meet — see who is in the call, mic/camera controls, and a speaking indicator.
- **UI (`index.html` + `app.js` rewrite):** tile grid (one tile per participant: caller, support, AI agent), bottom control bar (mic / camera / hang up / people toggle with count badge), right-side People panel with roles + mic state, avatar fallback when a camera is off, blue speaking ring + animated bars via `ActiveSpeakersChanged`, and a "You left the call / Call ended" screen with rejoin. Buttons turn red when muted. Dual-mode contract preserved: `?token=` synthetic-track harness and `?session=` Google sign-in are untouched, as are `window.testState` / `window.testActions` (extended with `toggleMic`/`toggleCamera`/`hangUp`/`togglePeople`/`getParticipants` and `testState.participants`). `#remoteVideo`/`#localVideo` ids are kept on live video elements because the e2e harness inspects them. Gotcha fixed during verification: CSS `display` rules on overlays were overriding the `hidden` attribute (`[hidden]{display:none !important}`), and `room.participants` doesn't exist in livekit-client 2.x (`remoteParticipants`).
- **Reconnect product bug (real):** the page's `reconnectPolicy` was a plain `{maxRetries, retryDelay}` object, but livekit-client 2.x requires a policy implementing `nextRetryDelayInMs()`. Any real signal drop made the engine throw "encountered error in reconnect policy" and give up after 0 attempts — the browser client could never recover from a WiFi drop. Replaced with a valid policy (25 retries, 1s→10s capped exponential backoff).
- **Reconnect test simulation:** proved via an instrumented probe that Playwright's `set_offline` blocks new HTTP requests but leaves the established signal WebSocket and WebRTC media flowing, so LiveKit's reconnect loop never triggered; recovery hinged on livekit-client's single-shot `online`-event HEAD probe, which races Chromium's network restore (fail → silent zombie). `test_lifecycle_reconnect` now closes the engine's signal socket from inside the page (`room.engine.client.ws.close()` + `onClose('')`, the same mechanism livekit-client's own connection checker uses) while `set_offline` blocks reconnection for 5s — deterministic 3/3 isolated passes in ~15s each.
- **Agent job shedding on shared hosts:** the support agent worker refused new jobs ("worker is at full capacity") because livekit-agents' prod `load_threshold` (0.7) measures whole-system CPU and this dev box is shared. `AGENT_LOAD_THRESHOLD` / `TRANSLATION_LOAD_THRESHOLD` (default 0.95) now configure both workers (`agent/agent/main.py`, `agent/agent/translation_worker.py`).
- **Chat panel:** People/Chat tabs in the side panel (Meet-style); text chat over a LiveKit data channel (`publishData`, topic `chat`, 3-participant cap fits in one packet). Bubbles with name + HH:MM, own messages right-aligned, unread badge on the chat button (only while the panel is closed/on People tab), Enter-to-send. History is per-page-load (LiveKit chat is ephemeral). Gotcha: the 2.x JS API is `publishData`, not `sendData`.
- **Verification:** reconnect 3/3 isolated; UI smoke 15/15 (tiles, people panel, mic/cam toggles propagate to remote side, speaking indicator, end card) with screenshots; agent greeting + caller-speech tests green after worker restart; auth page through the tunnel still blocked only at `auth/unauthorized-domain` (Firebase console step).
- **Docs:** `docs/12-user-guide.md` browser section describes the Meet-style controls.

### Browser Google sign-in + short session-id join links

- **Goal:** support operators join from a browser via a short `/join/<session_id>` link — no tokens in URLs. Google sign-in mints LiveKit credentials after authentication.
- **Backend:**
  - `JoinRequest` now accepts exactly one of `join_code` / `session_id` (`session_id` joins skip the 6-char code TTL, since operator links outlive codes).
  - New `GET /v1/sessions/public-config` serves the Firebase Web SDK config (`FIREBASE_WEB_API_KEY`, derived auth domain; key is not secret). Config setting + `.env.example` added; key sourced from `google-services.json`.
  - New `GET /join/{session_id}` → 302 to `/support-web/?session=<id>`.
  - New `Cache-Control: no-cache` middleware for `/support-web/*` — Cloudflare's default 4h .js caching served a stale `app.js` through the tunnel after deploys; index.html now versions the script (`app.js?v=...`, bump on change).
- **Web client (`support-web`):** dual mode. `?token=` keeps the synthetic-track harness path unchanged; `?session=` shows "Sign in with Google" (Firebase popup), POSTs `/v1/sessions/join` with the Firebase ID token, then connects with **real camera/mic**. Backend error codes map to friendly messages (allowlist, role occupied, session gone).
- **Verification:** backend 48/48 (4 new tests), ruff/mypy clean, OpenAPI regenerated + drift clean; auth page verified through the tunnel with Playwright up to the Firebase boundary, which rejects with `auth/unauthorized-domain` until `remote-support.lgitech.net` is added in Firebase Console → Auth → Authorized domains (localhost works out of the box).
- **Open item for user:** add the tunnel host as an authorized domain in the Firebase console; sign in with an `@lgitech.net` Google account (matches `SUPPORT_ALLOWED_DOMAINS`).

### Headless web-client lifecycle / dialogue / recording harness

- **Goal:** Get the local E2E suite green for two headless web clients with synthetic A/V, lifecycle/reconnect, agent transcription/dialogue, and recording verification.
- **LiveKit Cloud Inference TTS blocker:** Every TTS provider/voice tried failed with `no audio frames were pushed`. Added `agent/agent/dummy_tts.py` (`ToneTTS`) — a local sine-wave TTS shim implementing the `tts.TTS` interface so the agent speech scheduler keeps moving and text replies still reach the transcript sink. Controlled by `USE_DUMMY_TTS=true` (default) in `agent/agent/config.py`.
- **Agent wiring fix:** Moved `_wire_transcript_sources`, `SupportTranscriber.watch`, and `ControlPlane.attach` **before** the proactive SOLO greeting in `agent/agent/main.py`. The previous order registered event handlers after `session.generate_reply()`, so the greeting and early caller STT were lost.
- **Proactive greeting restored:** Re-enabled the SOLO-mode proactive greeting now that the ToneTTS shim and wiring order are correct.
- **Web client speech fixture fix:** Changed `backend/app/static/support-web/app.js` so the caller speech fixture no longer loops continuously. It now plays the clip twice with an 8-second silence gap, giving the agent's STT pipeline a fresh chance to transcribe even if the agent joins after the first utterance has started.
- **E2E results (local, `BACKEND_URL=http://localhost:8000`):**
  - `test_two_web_clients` ✅
  - `test_lifecycle_reconnect` ✅
  - `test_agent_joins_and_greets` ✅
  - `test_caller_speech_agent_response` ✅ (caller speech transcribed as "Hello, agent."; agent replied)
  - `test_recordings_and_transcript_after_session_end` ⏭️ skipped on localhost
- **Recording/egress gap:** LiveKit Cloud delivers `track_published`, `egress_ended`, and `room_finished` webhooks to the backend URL. When that URL is `localhost`, the cloud service cannot reach it, so egress never starts and the post-room transcript export is never triggered. Added a localhost skip to the test with an explanatory docstring. This test should be run against the deployed backend (`https://remotesupport.lgitech.net`).
- **Assumption/decision logged:** `USE_DUMMY_TTS=true` is the default so the agent remains functional while LiveKit Cloud Inference TTS is broken. Set it to `false` to re-test cloud TTS once the provider issue is resolved.
- **Next:** Update `docs/02-architecture.md`, `docs/12-user-guide.md`, and `README.md` with the new web-client regression flow, the TTS fallback, and the localhost recording limitation.

### Previous turn summary

- Implemented the approved two-web-client automated regression harness.
- Added `backend/app/routers/testing.py` with `POST /internal/test/support-token` (service-key gated, `ALLOW_TEST_ENDPOINTS=true`).
- Added `backend/app/static/support-web/` (HTML/JS client using LiveKit JS SDK UMD from jsDelivr).
- Updated `backend/app/main.py` to conditionally include the testing router and mount `/support-web/` static files.
- Added `tests/e2e/` with `requirements.txt`, `conftest.py`, `.env.example`, and `test_two_web_clients.py`.
- Updated `backend/.env.example` and `backend/.env` with `ALLOW_TEST_ENDPOINTS=true`.
- Verified locally: backend tests 44/44, ruff/mypy clean, OpenAPI drift clean, and the Playwright two-web-client test passes against a local backend with real LiveKit Cloud credentials.
- Deployed to production after the user refreshed `gcloud auth login`: built linux/amd64 image locally, pushed to Artifact Registry, copied as a tar to `remote-support-vm`, loaded with `docker load`, and restarted the backend container.
- Verified production: `https://remotesupport.lgitech.net/healthz` and `/readyz` healthy, `/support-web/` returns 200, and the Playwright two-web-client test passes against the live backend.
- Added `.github/workflows/e2e.yml` for nightly / workflow-dispatch regression runs against the live backend.
- Updated documentation: `docs/02-architecture.md` now covers the web client and regression harness; new `docs/12-user-guide.md` explains caller/support usage; `README.md` rewritten with architecture diagram, usage quick-start, test/run instructions, and production deployment notes.

## Open multi-party calls (2026-10-26)

- Added `backend/app/routers/calls.py`: `GET /call` (new Meet-style id), `GET /call/{id}` (redirect into the web client), `POST /v1/calls/{id}/join` (unauthenticated guest token, 30/min/IP rate limit). Gated by `OPEN_CALLS_ENABLED` (default false); `OPEN_CALL_MAX_PARTICIPANTS` (default 12).
- Rooms use the `call_` prefix, so guest tokens can never address support-session rooms (`rs_`). No DB rows, consent, recording, or agent dispatch; webhooks ignore these rooms.
- Web client: `?call=<id>` mode with a name prompt and a copy-link button; guests are labeled "Guest".
- `/support-web` is now mounted when either `ALLOW_TEST_ENDPOINTS` or `OPEN_CALLS_ENABLED` is set.
- Tests: `backend/tests/test_calls.py` (10 cases), `tests/e2e/test_open_call.py` (N headless browsers; passed with 3 and 5 guests via `remote-support.lgitech.net`).
- **Open-call recording:** `OPEN_CALL_RECORDING_ENABLED` arms LiveKit auto track egress at room creation, so each published track goes to its own file under `gs://$GCS_BUCKET/calls/<call_id>/` (`<time>_<identity>_<source>_<track_id>.{ogg,webm}` plus `EG_*.json` manifests). Guest identities now embed a name slug (`guest-alice-1a2b3c4d`) so files are readable. Added `GET /v1/calls/{id}` (recording flag for the pre-join notice) and a REC badge. Verified live: a 2-guest call produced 4 files (2x VP8 webm, 2x Opus ogg) that ffprobe reads cleanly.
- **Recording start/stop button (open calls):** Replaced auto-egress with on-demand recording (`backend/app/services/call_recording.py`). Any participant toggles it via `POST /v1/calls/{id}/recording/{start,stop,sync}`, authenticated with their own LiveKit call token. State is in room metadata; a per-room background reconcile loop (3 s) plus client `sync` nudges record late joiners/tracks. Per-room asyncio lock prevents duplicate egress (single backend process assumption). **LiveKit Cloud limit found:** the project caps concurrent egress at 5 (`resource_exhausted`, 429), so a 3rd person's 6th track is skipped; the UI shows a warning and the loop retries. Verified live (Alice starts, Bob joins late and stops; 4 files in GCS) and 62 backend tests pass.
- **Path-prefix hosting (`PUBLIC_BASE_PATH`):** `app/base_path.py` middleware sets ASGI `root_path` for requests under the prefix (Cloudflare Tunnel path rules cannot rewrite paths), redirects use `url_for_path`, and the web client derives `BASE` from its own URL. `GET /` redirects to a new call. Local `.env` uses `/livekit`; bare paths keep working. Live e2e (2 tests incl. recording) passes via `https://remote-support.lgitech.net/livekit`. **Pending (needs sudo + Cloudflare Zero Trust):** add a `path: ^/livekit(/.*)?$ -> http://localhost:8000` rule for `gx10-645d.lgitech.net` above its SSH rule in `/etc/cloudflared/config.yml`, and a Cloudflare Access Bypass app for `gx10-645d.lgitech.net/livekit` (the hostname is Access-protected).
- **Per-person recording + viewer (2026-10-26):** Recording switched from Track Egress to Participant Egress — one MP4 (H.264 720p + AAC) per person at `calls/<id>/<UTC>_<identity>.mp4`, halving egress-slot use. Added `GET /v1/calls/{id}/recordings` (link-only access by product decision; signed URLs) and a Recordings overlay in the web client (join screen button + control-bar button). Live e2e via `remote-support.lgitech.net/livekit` passes (2 people → 2 MP4s, viewer plays both). `BasePathMiddleware` made idempotent. 64 backend tests pass.
- **Hosting decision:** user wants the production GCP VM (`remotesupport.lgitech.net`) used again. Added `scripts/deploy-backend-gcp.sh` (builds on the VM, sets OPEN_CALLS_ENABLED / OPEN_CALL_RECORDING_ENABLED / PUBLIC_BASE_PATH=/livekit in backend.env, recreates only the backend container, smoke tests). Image verified to build and route correctly locally. **Blocked:** gcloud credentials on the GX10 have expired; needs `gcloud auth login`.
- **Device selection + mute-all (web client, desktop and phone browsers):** Real-mode capture now uses LiveKit-managed tracks (`setMicrophoneEnabled`/`setCameraEnabled` with deviceId) so `room.switchActiveDevice` works mid-call. Join screen has a camera preview and Microphone/Camera/Speaker pickers (speaker only where `setSinkId` exists — not iOS Safari); a gear button opens the same pickers in-call (plus View recordings, replacing the control-bar recordings button). New pause button mutes/unmutes mic+camera together. Choices persist in localStorage; rear cameras are not mirrored. Added phone layout (single-row control bar at 320–390px, stacked tiles in portrait, scrollable overlays). E2E: 5/5 open-call tests pass live (incl. device switch, mute-all seen by the other side, iPhone 13/SE layouts). `test_two_web_clients.py`: core A/V tests pass; agent/recording tests fail locally as before (no agent worker, webhooks point at prod).
- **In-call device menus:** Meet-style arrow on the mic/camera buttons opens a device menu (mic + speaker / camera) that switches live via `room.switchActiveDevice`; the gear sheet remains. Deployed to the GCP VM; 5/5 open-call e2e pass on `https://remotesupport.lgitech.net/livekit`.
- **Hang up for everyone:** red button in open calls now offers "Leave call" or "End call for everyone" (`POST /v1/calls/{id}/end`: stop egress, publish ended_by, delete room). Clients show who ended it. 66 backend tests; 6/6 open-call e2e pass on production after deploy.
- **Pre-existing prod failures (2026-10-26):** (1) Agent crashed on every job ("cannot access local participant before connecting") — fixed with `await ctx.connect()` before `session.start()`; agent redeployed to the VM via `scripts/deploy-backend-gcp.sh agent` (script now takes `backend|agent`). Agent e2e tests now pass on prod. (2) `POST /v1/sessions/{id}/end` 500'd when the LiveKit room was already gone — now treats 404 as success (+ regression test). (3) **Still open:** no LiveKit webhooks reach prod, so webhook-driven support-session recording/transcript export never runs. The LiveKit Cloud project webhook must be set (dashboard) to `https://remotesupport.lgitech.net/v1/webhooks/livekit`, signed with the backend's API key; the endpoint was verified with a self-signed event.
- **Local recording mirror:** `scripts/sync-recordings.py` copies `calls/` and `sessions/` from GCS to `/mnt/ext_primary/users/jcl5m1/remote-support-recordings/` (size-checked, temp-file + rename, never deletes). Runs every 5 min via systemd user timer `remote-support-recordings-sync.timer` (units in `infra/systemd/`, installed to `~/.config/systemd/user/`; skipped if the drive isn't mounted). GCS remains the primary copy. Verified: initial 118 files, then a new prod recording mirrored automatically by the timer.
