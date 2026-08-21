# 11 — Automated regression testing

> Headless web-client harness for verifying the end-to-end call flow without two physical mobile devices.

## What is implemented

The two-web-client Playwright harness is built and running:

- **`backend/app/static/support-web/`** — plain HTML + JS client using the LiveKit JS SDK UMD build from jsDelivr. Joins a room with `?token=<jwt>&ws_url=<wss://...>&identity=<name>`. Publishes synthetic video (canvas clock) and audio (tone or fetched speech file), subscribes to remote tracks, and exposes `window.testState` for Playwright.
- **`POST /internal/test/support-token`** — service-key-gated endpoint (only when `ALLOW_TEST_ENDPOINTS=true`) that mints a synthetic support token without Firebase Auth.
- **`tests/e2e/test_two_web_clients.py`** — pytest + Playwright suite.

## Test coverage

| Test | Verifies |
|---|---|
| `test_two_web_clients` | Two headless Chromium tabs connect, exchange A/V, and inbound bytes increase. |
| `test_lifecycle_reconnect` | Caller network is dropped for 5 s, the tab reconnects, and media resumes. |
| `test_agent_joins_and_greets` | AI agent joins after caller consent and produces an `agent_llm` transcript utterance. |
| `test_caller_speech_agent_response` | Caller publishes real speech audio; agent transcribes it and replies. |
| `test_recordings_and_transcript_after_session_end` | After the call, track/room-composite egress recordings and JSONL/VTT/TXT transcript exports exist. |

## Local run

```bash
cd tests/e2e
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# Edit .env: BACKEND_URL, SERVICE_API_KEY, LIVEKIT_URL

pytest test_two_web_clients.py -v
```

Use `BACKEND_URL=http://localhost:8000` for a local backend. The recording test is
skipped locally because LiveKit Cloud cannot deliver webhooks to `localhost`.

## Production / CI run

```bash
BACKEND_URL=https://remotesupport.lgitech.net pytest test_two_web_clients.py -v
```

`.github/workflows/e2e.yml` runs the suite nightly and on `workflow_dispatch`
against the live backend using repository secrets.

## Synthetic media details

- **Video:** canvas drawing a running clock with milliseconds and the identity label, captured at 20 fps.
- **Audio (default):** sine tone whose frequency slowly modulates.
- **Audio (speech test):** `tests/e2e/fixtures/caller_prompt.wav` is fetched from a local fixture server and played twice with an 8-second silence gap so the agent's STT has a fresh utterance to transcribe.

## Agent TTS fallback

LiveKit Cloud Inference TTS currently fails in this project with
`no audio frames were pushed` across providers/voices. The agent defaults to
`USE_DUMMY_TTS=true` (`agent/agent/dummy_tts.py`): a local sine-wave TTS shim
that keeps the speech scheduler moving so text replies still reach the transcript
sink. The audio is not intelligible speech, but it makes the dialogue loop
testable. Set `USE_DUMMY_TTS=false` to re-test cloud TTS once the provider issue
is resolved.

## Open milestones

1. **Recording egress verification against the deployed backend.** The local suite
   skips this; run it against `https://remotesupport.lgitech.net` once webhooks are
   configured in LiveKit Cloud.
2. **Android + web variant.** Replace one browser tab with the Android app launched
   via ADB deep link, keeping the support web client in Playwright.
3. **Lifecycle stress tests.** Longer runs with repeated reconnects, support
   joining/leaving, and AI toggle flips.
