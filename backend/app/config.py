"""Application settings, loaded from environment. See backend/.env.example."""

import base64
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LiveKit -------------------------------------------------------
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_agent_name: str = "support-agent"

    # --- Database ------------------------------------------------------
    database_url: str

    # --- Google Cloud Storage ------------------------------------------
    gcs_bucket: str
    # Base64 of the service-account JSON. LiveKit Egress needs the credential
    # itself, because *it*, not this backend, writes the objects.
    #
    # Base64 rather than raw JSON: the private key contains escaped newlines
    # that .env files and shell tooling readily expand into real newlines,
    # which silently truncates the value at the first line break.
    gcp_credentials_b64: str
    signed_url_ttl_seconds: int = 900

    # --- Auth ----------------------------------------------------------
    caller_jwt_secret: str
    caller_jwt_ttl_seconds: int = 3600
    service_api_key: str

    # Support operators sign in with Google SSO via Firebase Auth. Firebase
    # issues the token; the ``admin`` custom claim gates the purge endpoint.
    firebase_project_id: str
    # Firebase Web SDK API key for browser Google sign-in (served to the web
    # client via /v1/public-config). Not a secret -- it ships in client JS.
    firebase_web_api_key: str = ""
    # Comma-separated. At least one of domains/emails MUST be set -- an empty
    # allowlist would let any Google account claim the trusted support role.
    support_allowed_domains: str = ""
    support_allowed_emails: str = ""
    support_admin_emails: str = ""

    # --- Session policy ------------------------------------------------
    join_code_length: int = 6
    join_code_ttl_seconds: int = 1800
    livekit_token_ttl_seconds: int = 900
    room_empty_timeout_seconds: int = 300
    room_departure_timeout_seconds: int = 60
    room_max_duration_seconds: int = 7200
    idle_session_timeout_seconds: int = 900

    # --- Consent & retention -------------------------------------------
    consent_text_version: str = "v1.0"
    allow_unrecorded_fallback: bool = False
    allow_caller_download: bool = False
    retention_days: int = 30

    # --- App links -----------------------------------------------------
    app_link_host: str = "support.example.com"
    deep_link_scheme: str = "remotesupport"

    # --- Local development ---------------------------------------------
    # When true, the backend starts even if LiveKit/GCS credentials are missing,
    # using stub clients that fail on actual API calls. /readyz then reports
    # those dependencies unhealthy while postgres can still be healthy.
    allow_degraded_start: bool = False

    # --- Internal test endpoints ---------------------------------------
    # When true, exposes /internal/test/* endpoints used by the automated
    # regression harness. These endpoints bypass production auth and must never
    # be enabled in a public-facing deployment.
    allow_test_endpoints: bool = False

    # Optional path prefix when served behind a shared hostname, e.g.
    # "/livekit" for https://gx10-645d.lgitech.net/livekit/. The proxy forwards
    # the prefix unchanged; the app serves both prefixed and bare paths.
    public_base_path: str = ""

    # --- Open calls ----------------------------------------------------
    # When true, anyone with a /call/<call_id> link can join a plain
    # multi-party A/V room (no sign-in, no consent, no recording, no agent).
    # Open-call rooms use a separate ``call_`` prefix so a guest token can never
    # reach a support session room (``rs_``).
    open_calls_enabled: bool = False
    open_call_max_participants: int = 12
    # When true, any open-call participant can start/stop recording; while on,
    # every published audio/video track is saved as its own file in GCS_BUCKET
    # under calls/<call_id>/.
    open_call_recording_enabled: bool = False

    # --- Rate limits ---------------------------------------------------
    utterance_rate_limit: int = 600
    utterance_rate_limit_window_seconds: int = 60

    # --- Misc ----------------------------------------------------------
    log_level: str = "INFO"
    environment: str = "development"

    @staticmethod
    def _csv_set(raw: str) -> frozenset[str]:
        return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())

    @property
    def firebase_auth_domain(self) -> str:
        return f"{self.firebase_project_id}.firebaseapp.com"

    @property
    def support_allowed_domain_set(self) -> frozenset[str]:
        return self._csv_set(self.support_allowed_domains)

    @property
    def support_allowed_email_set(self) -> frozenset[str]:
        return self._csv_set(self.support_allowed_emails)

    @property
    def support_admin_email_set(self) -> frozenset[str]:
        return self._csv_set(self.support_admin_emails)

    @property
    def gcp_credentials_json(self) -> str:
        """Raw service-account JSON, as LiveKit Egress' GCPUpload expects it."""
        return base64.b64decode(self.gcp_credentials_b64).decode()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
