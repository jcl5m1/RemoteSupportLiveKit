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
