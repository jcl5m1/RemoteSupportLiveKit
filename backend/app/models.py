"""SQLAlchemy models. Schema reference: docs/03-data-model.md.

STUB: enums and columns are declared to match the doc; relationships,
constraints and the Alembic migration are Phase 1 work.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionState(str, enum.Enum):
    PENDING = "pending"
    CONSENT_DECLINED = "consent_declined"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    PURGED = "purged"


class ParticipantRole(str, enum.Enum):
    CALLER = "caller"
    SUPPORT = "support"
    AGENT = "agent"


class AgentMode(str, enum.Enum):
    SOLO = "SOLO"
    ASSISTED = "ASSISTED"
    WRAP_UP = "WRAP_UP"


class RecordingKind(str, enum.Enum):
    TRACK_VIDEO = "track_video"
    TRACK_AUDIO = "track_audio"
    ROOM_COMPOSITE = "room_composite"


class RecordingState(str, enum.Enum):
    STARTING = "starting"
    ACTIVE = "active"
    ENDING = "ending"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"


class UtteranceSource(str, enum.Enum):
    AGENT_STT = "agent_stt"      # caller speech via AgentSession
    SUPPORT_STT = "support_stt"  # support speech via parallel stream
    AGENT_LLM = "agent_llm"      # the agent's own reply


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    room_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    state: Mapped[SessionState] = mapped_column(
        Enum(
            SessionState,
            name="session_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SessionState.PENDING,
    )

    join_code: Mapped[str | None] = mapped_column(String(6))
    join_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    caller_identity: Mapped[str | None] = mapped_column(Text)
    support_identity: Mapped[str | None] = mapped_column(Text)
    support_user_id: Mapped[str | None] = mapped_column(Text)

    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    agent_mode: Mapped[AgentMode] = mapped_column(
        Enum(
            AgentMode,
            name="agent_mode",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=AgentMode.SOLO,
    )
    metadata_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Zero point for every transcript offset. Written once, never updated.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcript_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # A code only needs to be unique among sessions that can still be joined.
        Index(
            "ux_sessions_active_join_code",
            "join_code",
            unique=True,
            postgresql_where=text("state IN ('pending','active') AND join_code IS NOT NULL"),
        ),
        Index("ix_sessions_state_created", "state", "created_at"),
    )


class SessionParticipant(Base):
    __tablename__ = "session_participants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(
            ParticipantRole,
            name="participant_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Enforces FR-1.8: one caller, one support, one agent per session.
        Index("ux_participants_session_role", "session_id", "role", unique=True),
    )


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    egress_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[RecordingKind] = mapped_column(
        Enum(
            RecordingKind,
            name="recording_kind",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    role: Mapped[ParticipantRole | None] = mapped_column(
        Enum(
            ParticipantRole,
            name="participant_role",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    track_sid: Mapped[str | None] = mapped_column(Text)
    state: Mapped[RecordingState] = mapped_column(
        Enum(
            RecordingState,
            name="recording_state",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    gcs_uri: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_recordings_session", "session_id"),)


class TranscriptUtterance(Base):
    __tablename__ = "transcript_utterances"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    # Agent-generated ULID. Makes the ingest endpoint idempotent so the agent's
    # retry loop is free.
    client_utterance_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[ParticipantRole] = mapped_column(
        Enum(
            ParticipantRole,
            name="participant_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[UtteranceSource] = mapped_column(
        Enum(
            UtteranceSource,
            name="utterance_source",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    # Denormalized so the exported transcript is self-explaining without a join.
    agent_mode: Mapped[AgentMode | None] = mapped_column(
        Enum(
            AgentMode,
            name="agent_mode",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    ai_enabled: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ux_utterance_idem", "session_id", "client_utterance_id", unique=True),
        Index("ix_utterance_session_time", "session_id", "start_ms"),
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ConsentEvent(Base):
    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    consent_text_version: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DataPurge(Base):
    __tablename__ = "data_purges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    objects_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    utterances_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RateLimit(Base):
    __tablename__ = "rate_limits"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_rate_limits_window", "window_start"),)
