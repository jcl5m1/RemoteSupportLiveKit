"""Prometheus metrics for the backend."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

SESSIONS_CREATED = Counter(
    "remote_support_sessions_created_total",
    "Sessions created",
    ["recording_enabled"],
)

JOINS_ATTEMPTED = Counter(
    "remote_support_joins_attempted_total",
    "Support join attempts",
    ["result"],
)

CONSENT_DECISIONS = Counter(
    "remote_support_consent_decisions_total",
    "Consent accept/decline decisions",
    ["accepted"],
)

UTTERANCES_INGESTED = Counter(
    "remote_support_utterances_ingested_total",
    "Transcript utterances ingested",
    ["role", "source"],
)

TRANSCRIPT_BATCHES = Counter(
    "remote_support_transcript_batches_total",
    "Transcript ingest batches by outcome",
    ["status"],
)

AI_TOGGLES = Counter(
    "remote_support_ai_toggles_total",
    "AI enable/disable toggles",
    ["enabled"],
)

EGRESS_EVENTS = Counter(
    "remote_support_egress_events_total",
    "Egress webhook events",
    ["kind", "state"],
)

ACTIVE_SESSIONS = Gauge(
    "remote_support_active_sessions",
    "Sessions currently in active or pending state",
)

READYZ_DEPENDENCY_HEALTHY = Gauge(
    "remote_support_readyz_dependency_healthy",
    "Ready probe dependency health (1=ok, 0=error)",
    ["name"],
)

REQUEST_LATENCY = Histogram(
    "remote_support_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)
