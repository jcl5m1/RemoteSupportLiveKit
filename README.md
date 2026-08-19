# RemoteSupportLiveKit

[![CI](https://github.com/jcl5m1/RemoteSupportLiveKit/actions/workflows/ci.yml/badge.svg)](https://github.com/jcl5m1/RemoteSupportLiveKit/actions/workflows/ci.yml)

Two-party mobile support calling on LiveKit, with an AI voice agent that hears
both people but only ever speaks to one of them.

- **caller** — anonymous end user, joins with a 6-character code
- **support** — authenticated operator, controls the AI toggle
- **agent** — AI voice participant, responds to the caller only

Every session produces four separate media files (caller video, caller audio,
support video, support audio), one composited MP4, and a diarized transcript
covering all three speakers.

> **Status: implementation complete, validation in progress.** All seven phases
> of the spec are implemented with passing unit/widget tests. Backend, agent,
> and containerized stack are verified against real LiveKit/GCS credentials.
> Remaining work is mobile production packaging (bundle id, Firebase configs,
> iOS build) — see [`progress.md`](progress.md) for the live tracker and open
> blockers.

## Start here

Read [`docs/00-overview.md`](docs/00-overview.md), then
[`docs/09-implementation-plan.md`](docs/09-implementation-plan.md).

| Doc | Contents |
|---|---|
| [00 Overview](docs/00-overview.md) | Roles, locked decisions, why the agent is shaped this way |
| [01 Requirements](docs/01-requirements.md) | Numbered FR/NFR that tests reference |
| [02 Architecture](docs/02-architecture.md) | Components, lifecycle sequence, control plane |
| [03 Data model](docs/03-data-model.md) | Postgres schema, GCS layout, transcript format |
| [04 API contract](docs/04-api-contract.md) | Every endpoint, request/response, webhook table |
| [05 Agent design](docs/05-agent-design.md) | Modes, prompt layering, dual-STT, AI toggle |
| [06 Recording & transcripts](docs/06-recording-transcripts.md) | Egress plan, codec ADR, export |
| [07 Flutter app](docs/07-flutter-app.md) | Screens, state, deep links, permissions |
| [08 Security & compliance](docs/08-security-compliance.md) | Auth, consent, retention, threats |
| [09 Implementation plan](docs/09-implementation-plan.md) | 7 phases with acceptance criteria |
| [10 Risks & references](docs/10-risks-references.md) | Upstream issues, version pins, links |

## Layout

```
backend/    FastAPI — tokens, sessions, egress orchestration, transcripts
agent/      livekit-agents worker — the AI participant
mobile/     Flutter app — both roles in one binary
infra/      docker-compose, Dockerfiles, Prometheus alerts, Grafana dashboard
docs/       the specification and exported OpenAPI schema
scripts/    test-all, dev-start, load-test, env/mobile config checks, openapi export helpers
```

## Test status

| Component | Tests | Lint / Type check |
|---|---|---|
| Backend | 44/44 passing | `ruff` clean, `mypy` clean |
| Agent | 17/17 passing | `ruff` clean |
| Flutter | 11/11 passing | `flutter analyze` clean |

See [`progress.md`](progress.md) for environment blockers and open questions.

## Stack

Flutter · LiveKit Cloud · Python 3.12 / FastAPI · PostgreSQL · Google Cloud
Storage · Deepgram STT · Kimi K2.6 LLM · Cartesia TTS (all via LiveKit Cloud Inference)

## The one thing to get right

> The agent hears both humans but speaks to only the caller.

This is enforced **structurally**, not by prompting. `AgentSession` binds to the
caller via `RoomOptions.participant_identity`, so its turn detection and reply
generation never see support's audio. Support is transcribed by a separate
STT-only pipeline that has no code path to `generate_reply()`.

If you refactor the agent, keep that separation. Prompt instructions telling a
model to stay quiet are a second layer, never the mechanism —
[docs/05](docs/05-agent-design.md) explains why.

## Local development

The fastest way to run everything without Docker:

```bash
# 1. Bootstrap a fresh clone (checks Python 3.12, Postgres, creates venvs)
./scripts/setup.sh

# 2. Start Postgres locally, then run the stack
./scripts/dev-start.sh
```

`scripts/dev-start.sh` starts the backend and agent in degraded mode when
LiveKit/GCS credentials are missing, so the HTTP API and Postgres paths can be
exercised immediately.

Run all checks with:

```bash
./scripts/test-all.sh
```

Or use the pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
```

### Validate environment / configuration

```bash
# backend/agent .env and LiveKit/GCS connectivity
python scripts/check-env.py --prod --check-cloud

# mobile bundle id, universal links, Firebase config files
python scripts/check-mobile-config.py
```

### With Docker

The repository is tested with Colima + Docker CLI on macOS:

```bash
brew install colima docker docker-compose
mkdir -p ~/.docker
echo '{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker/config.json
colima start --cpu 4 --memory 8 --disk 60
```

With real cloud credentials in `backend/.env` and `agent/.env`:

```bash
docker compose -f infra/docker-compose.yml up --build
```

LiveKit is Cloud-hosted, so there is no local SFU. Webhooks need a publicly
reachable backend — tunnel port 8000 and point the LiveKit Cloud webhook config
at `https://<tunnel>/v1/webhooks/livekit`. Track egress is webhook-driven, so
recording phases will not work locally without this.

### Mobile

```bash
cd mobile && flutter pub get && flutter run
```

The Android release build is validated locally (JDK 21 + Android SDK 35/36).
iOS device builds require the full Xcode IDE; the Command Line Tools are not
sufficient.

Before release, apply production identifiers:

```bash
python scripts/apply-mobile-config.py \
  --bundle-id com.mycompany.remote.support \
  --app-group group.com.mycompany.remote.support \
  --universal-link-domain support.mycompany.com
```

Then drop `google-services.json` into `mobile/android/app/` and
`GoogleService-Info.plist` into `mobile/ios/Runner/` from the Firebase Console.

## Before writing agent or egress code

`livekit-agents` and the egress request classes have both moved between
releases. Pin the versions in the pyproject files, then verify the actual
signatures against what you installed:

```bash
python -c "import livekit.agents as a; help(a.AgentSession)"
python -c "import livekit.api as a; help(a.TrackEgressRequest)"
```

[docs/10](docs/10-risks-references.md) lists the specific drift and the open
upstream issues this design already routes around.
