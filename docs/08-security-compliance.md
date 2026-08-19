# 08 — Security & Compliance

## Trust model

The caller is anonymous and therefore untrusted. The support user is
authenticated and semi-trusted. The agent worker is trusted infrastructure.
Everything the backend does assumes a client may be hostile.

Concretely, that means:

- **Roles are server-assigned.** A client asks to create a session or to join
  with a code; it never states its role. The role is embedded in the signed
  LiveKit token metadata and in the identity prefix.
- **No client ever holds a LiveKit API secret**, GCS credential, or AI provider
  key. Clients hold only short-lived room tokens and (for support) an IdP JWT.
- **No client gets `room_admin`.** Room metadata — which carries the AI toggle
  and recording state — is mutated only by the backend with API-key credentials.
  A client with `can_update_own_metadata` could otherwise fake the recording
  indicator.
- **The agent re-authorizes the fast-path toggle locally.** The
  `rs.agent.control` data message bypasses the backend, so the agent checks the
  sender's identity prefix before applying it. Without that check a caller could
  mute the agent.

## Support authentication — Google SSO via Firebase

Support operators sign in with Google through Firebase Auth. Callers are
unaffected and remain anonymous.

**Why Firebase rather than raw Google Sign-In.** Google's own ID token carries
`aud, email, email_verified, exp, family_name, given_name, iat, iss, name,
picture, sub` — no roles. `DELETE /v1/sessions/{id}/data` needs an `admin`
claim, so raw Google Sign-In would require a `support_users` table and a join on
every request. Firebase issues its own token with server-set **custom claims**,
making the admin gate a single JWT field. It also refreshes tokens
automatically, which matters because Google ID tokens expire hourly and support
operators routinely stay on calls longer than that.

**Verification** (`app/core/firebase_auth.py`):

| Check | Value |
|---|---|
| Algorithm | RS256 only |
| JWKS | `https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com` |
| Issuer | `https://securetoken.google.com/{FIREBASE_PROJECT_ID}` |
| Audience | `{FIREBASE_PROJECT_ID}` |
| Required claims | `exp`, `iat`, `sub`, `aud`, `iss` |
| Clock skew | 30 s leeway |

Signing keys are cached by `PyJWKClient` with a 1-hour lifespan, so a
steady-state request makes no outbound call.

**Authorization gate.** Signature validity only proves *a* Google account. It
does not prove an *authorized* one. On top of verification we require:

1. `email_verified` is true — an unverified email is spoofable, so the domain
   check would prove nothing without this.
2. The email's domain is in `SUPPORT_ALLOWED_DOMAINS`, or the address is in
   `SUPPORT_ALLOWED_EMAILS`.

Domain matching is **exact**, not suffix-based: `evil-lgitech.net`,
`lgitech.net.evil.com`, and `sub.lgitech.net` are all rejected against an
allowlist of `lgitech.net`.

We gate on the verified email domain rather than Google's `hd` claim. `hd` is
absent for consumer accounts and is not enumerated in Google's
`claims_supported`, so it is captured for audit but never used as the gate.

**Fails closed.** If neither allowlist is configured, every request is rejected
with `no_allowlist_configured`. A misconfigured deployment must admit nobody
rather than everybody. This is covered by
`backend/tests/test_support_auth.py::test_fails_closed_with_no_allowlist`.

**Admin.** `is_admin` comes from the `admin` custom claim, set server-side via
the Firebase Admin SDK. `SUPPORT_ADMIN_EMAILS` is a bootstrap path for the first
operator; clear it once real claims are set.

**Client-side `hostedDomain`** in `GoogleSignIn` filters the account picker for
convenience only. It is not a security control — the backend re-checks on every
request.

## Join-code security

A 6-character code over a 28-symbol alphabet is ~481M combinations. That is not
cryptographically strong, so the security comes from the surrounding controls:

- Codes are valid only while the session is `pending`/`active`, and expire after
  `JOIN_CODE_TTL_SECONDS` (default 1800).
- A session accepts exactly one support participant (`ux_participants_session_role`).
  Guessing a live code races against the legitimate operator and fails once they
  join.
- `POST /v1/sessions/join` is rate-limited to 10 attempts/min/user and 100/hour/IP,
  with exponential backoff after 5 consecutive failures.
- Joining requires support authentication. An anonymous attacker cannot brute
  force the endpoint at all; the threat is a *malicious authenticated operator*,
  which is an insider-threat problem addressed by the audit log rather than by
  code entropy.

If the threat model later includes untrusted support users, lengthen the code to
8 characters and add a per-session nonce to the deep link.

## Consent

The consent gate is enforced at the point of token issuance, not in the UI.
`POST /v1/sessions` returns no LiveKit token; only `POST /v1/sessions/{id}/consent`
with `accepted: true` does. A client that skips the consent screen simply has no
way to connect.

`consent_events` records the decision, timestamp, client IP, user agent, and the
`consent_text_version` string that was displayed. Version the consent text and
never mutate a published version — if the wording changes, mint `v1.1`. This is
what makes an old consent record meaningful years later.

Two-party consent jurisdictions require the *support* side to be on notice too.
Support sees the REC indicator, and their acceptance is implicit in signing in to
a system whose terms disclose recording. If you operate somewhere requiring
explicit dual consent, add a support-side acknowledgement at sign-in — the
`consent_events` table already accommodates it via a nullable role column.

**This document is engineering guidance, not legal advice.** Recording laws vary
by jurisdiction; have counsel review the consent text and the retention period
before launch.

## Data protection

| Concern | Control |
|---|---|
| Media at rest | GCS default encryption; consider CMEK for regulated data |
| Media in transit | Egress writes over TLS; clients fetch via V4 signed URLs only |
| Signed URL lifetime | `SIGNED_URL_TTL_SECONDS`, default 900 |
| Bucket exposure | Uniform bucket-level access, no `allUsers`, public access prevention **enforced** |
| Transcript at rest | Postgres with encryption at rest; consider column encryption for `text` if calls carry PII |
| Secrets | Secret Manager (or equivalent); never in the repo, never in client bundles |
| Logs | `session_id` is fine to log; **never log transcript text, tokens, or join codes** |

## Retention & deletion

- **Automatic:** a GCS lifecycle rule deletes objects under `sessions/` after
  `RETENTION_DAYS` (default 30). See `infra/gcs-lifecycle.json`.
- **Transcript rows** are pruned by a scheduled job on the same horizon, so the
  database doesn't outlive the media it describes.
- **On request:** `DELETE /v1/sessions/{id}/data` purges GCS objects, transcript
  rows, and exports; writes a `data_purges` tombstone; sets `state='purged'`.
  Requires an `admin` claim.

The tombstone is deliberate. Deleting the session row entirely would make the
audit log lie by omission — you could not distinguish "never existed" from
"deleted on request." Keep the skeleton, drop the content.

## Rate limits

| Endpoint | Limit |
|---|---|
| `POST /v1/sessions` | 5/min/IP, 20/hour/device_id |
| `POST /v1/sessions/join` | 10/min/user, 100/hour/IP |
| `POST /v1/sessions/{id}/consent` | 5/min/session |
| `POST /v1/sessions/{id}/agent` | 30/min/session |
| `POST /v1/sessions/{id}/utterances` | 600/min/session (service tier) |

Implement with a Redis token bucket if you already run Redis; otherwise a
Postgres-backed counter is adequate at this scale.

## Webhook verification

Use the LiveKit SDK's `WebhookReceiver` to verify the signature on every
`/v1/webhooks/livekit` request. Reject unverified requests with `401` before
parsing the body. Handlers must be idempotent — LiveKit retries, and a duplicate
`track_published` must not start a second egress. Key idempotency on
`(session_id, track_sid)` for egress starts and on `egress_id` for state updates.

## Abuse considerations

- A caller could create sessions in a loop to burn agent minutes. The
  `POST /v1/sessions` rate limit plus a per-device daily cap handles this;
  additionally, do not dispatch the agent until consent is granted, so
  abandoned sessions cost nothing.
- A session with a caller but no support and no activity should auto-terminate.
  Set `empty_timeout` and `departure_timeout` on the room, and add a sweeper that
  ends `active` sessions with no media for `IDLE_SESSION_TIMEOUT_SECONDS`
  (default 900).
- Agent cost per session is bounded by the room's `max_duration`. Set it
  (default 2 hours) so a forgotten call cannot run indefinitely.
