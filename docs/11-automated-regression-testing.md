# Automated regression testing for two-way A/V calls

> Plan for verifying the end-to-end call flow without relying on two physical mobile devices.

## Goal

Run an automated regression test that confirms:

1. A support client can join a session created by a caller.
2. Both sides publish and receive audio + video tracks.
3. Media bytes actually flow in both directions (not just a successful room join).
4. The session can be ended and the LiveKit room is cleaned up.

## Background

The current test suite covers:

- Backend unit/integration tests (real LiveKit/GCS)
- Agent unit tests
- Flutter widget/unit tests
- Android deep-link launch on a physical device

What is missing is a **two-party media test**. The blockers are:

- The Flutter mobile app requires a physical device or emulator.
- There is no support client other than the mobile app itself.

## Recommended approach: headless web-client test harness

The fastest path to full regression coverage is a **web-based support client** plus a **Playwright-driven test harness**. Web is easier to automate than Android and uses the same LiveKit Cloud infrastructure.

### Architecture

```text
┌─────────────────┐      create session       ┌──────────────────┐
│  Test harness   │ ────────────────────────▶ │  GCP backend     │
│  (Playwright)   │                           │  remotesupport.  │
└────────┬────────┘                           │  lgitech.net     │
         │                                    └────────┬─────────┘
         │                                             │
         │ 1. open caller page   2. open support page  │
         │    ( LiveKit JS SDK ) ( LiveKit JS SDK )    │
         ▼                                                 ▼
┌─────────────────┐                           ┌──────────────────┐
│  Caller client  │ ◀──── audio/video ──────▶ │  Support client  │
│  (browser tab)  │      (via LiveKit Cloud)  │  (browser tab)   │
└─────────────────┘                           └──────────────────┘
```

### Why Playwright + LiveKit JS SDK

- Playwright can launch multiple browser contexts/tabs from one Node/Python process.
- It supports fake media devices (`--use-fake-device-for-media-stream`), so no real camera/mic are needed.
- Assertions can check that remote `<video>` elements exist, are playing, and have non-zero dimensions.
- Network/RTC stats can be read from `RTCPeerConnection.getStats()` to verify byte counts increase.
- The same harness can run in CI (GitHub Actions) with `xvfb` or headless Chromium.

## Implementation plan

### Phase 1 — Minimal web support client

Create a small static web client that can join a session as the **support** role.

- **Location:** `web-client/` (new top-level directory) or served from `backend/app/static/support-web/`.
- **Stack:** plain HTML + TypeScript + `@livekit/components-react` or raw `livekit-client`.
- **Inputs:** `?code=XXXXXX` URL parameter.
- **Auth:** for regression, use a service-key-gated backend endpoint that mints a support token without Firebase. This endpoint must be restricted in production (e.g., only when `ALLOW_TEST_ENDPOINTS=true`).
- **UI:** single page with local preview, remote video, mute/camera-off/leave controls.

### Phase 2 — Backend test endpoint (optional but recommended)

Add a private endpoint for the harness:

```http
POST /internal/test/support-token
X-Service-Key: {SERVICE_API_KEY}
{
  "session_id": "..."
}
```

It returns the same payload as `POST /v1/sessions/{id}/support/token` but bypasses Firebase. This keeps the web client simple and avoids managing Firebase web auth in the test.

### Phase 3 — Playwright regression test

Create `tests/e2e/` (Python with `pytest-playwright` or Node with `@playwright/test`).

Test flow:

1. Create a caller session via backend API (`POST /v1/sessions`).
2. Open **caller page** in browser tab A at `https://remotesupport.lgitech.net/j/{code}`.
   - For the caller role the web client must first record consent and then join with the caller token.
3. Open **support page** in browser tab B with the support token from step 2 endpoint.
4. Wait for both sides to report `RoomEvent.Connected`.
5. Wait for each side to have a remote `<video>` element with `readyState >= 2` and `videoWidth > 0`.
6. Poll `getStats()` on both peers and assert that `bytesReceived` increases for audio and video tracks.
7. End the session via backend API (`POST /v1/sessions/{id}/end`).
8. Assert the LiveKit room is deleted (via backend or LiveKit API).

### Phase 4 — Android + web variant

Once the web harness works, extend it to replace one browser tab with the Android device:

- Use ADB to launch the Android app with a deep link (`remotesupport://join?code=...` as caller).
- Use Playwright for the support web client.
- Verify media flow by checking:
  - Web client remote video element (from Android camera).
  - Android logcat for `livekit_client` track events and connection state.

This is more fragile than two-web because Android UI automation is limited, but it validates the actual production client.

## Alternative: Flutter web support client

Instead of a separate JS client, build the mobile app for web (`flutter build web`) and load it in Playwright. Trade-offs:

- **Pros:** reuses existing Flutter code; tests the same UI logic.
- **Cons:** `livekit_client` Flutter plugin has limited/weaker web support; camera/mic permission flows differ; larger build artifacts.

A JS client is recommended for the harness because it is smaller, faster, and maps directly to the LiveKit web SDK that LiveKit actively maintains.

## CI integration

Add a GitHub Actions job that:

1. Checks out the repo.
2. Installs Node + Playwright browsers.
3. Optionally deploys the web client to the existing GCP backend (or serves it locally behind a tunnel).
4. Runs the regression test against `https://remotesupport.lgitech.net`.
5. Runs only on `workflow_dispatch` or nightly (not every PR) because it requires live cloud credentials and a running backend.

## Open questions / decisions

1. Do you want the web client deployed to the GCP backend (`/support-web/`) or hosted separately (Firebase Hosting / Cloud Run)?
2. Is it acceptable to add a service-key-gated `/internal/test/support-token` endpoint for the harness, or do you prefer full Firebase Auth in the web client?
3. Should the first milestone be **two-web clients** (fully autonomous) or **Android + web** (uses the real device we already have connected)?

## Suggested first milestone

Build the two-web-client Playwright harness. It unblocks automated regression the fastest and does not depend on the Android device being connected. Once that is green, add the Android + web variant as a second milestone.
