"""Initial schema."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260818_0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_enum(name: str, values: list[str]) -> None:
    # PostgreSQL does not support CREATE TYPE IF NOT EXISTS, so use a PL/pgSQL
    # block. This makes the migration idempotent and avoids Alembic/asyncpg
    # transactional DDL quirks.
    values_sql = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type WHERE typname = '{name}'
            ) THEN
                CREATE TYPE {name} AS ENUM ({values_sql});
            END IF;
        END
        $$
        """
    )


def upgrade() -> None:
    _create_enum(
        "session_state",
        ["pending", "consent_declined", "active", "completed", "failed", "purged"],
    )
    _create_enum("participant_role", ["caller", "support", "agent"])
    _create_enum("agent_mode", ["SOLO", "ASSISTED", "WRAP_UP"])
    _create_enum("recording_kind", ["track_video", "track_audio", "room_composite"])
    _create_enum(
        "recording_state",
        ["starting", "active", "ending", "complete", "failed", "aborted"],
    )
    _create_enum("utterance_source", ["agent_stt", "support_stt", "agent_llm"])

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("room_name", sa.Text(), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                "pending",
                "consent_declined",
                "active",
                "completed",
                "failed",
                "purged",
                name="session_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("join_code", sa.String(length=6), nullable=True),
        sa.Column("join_code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("caller_identity", sa.Text(), nullable=True),
        sa.Column("support_identity", sa.Text(), nullable=True),
        sa.Column("support_user_id", sa.Text(), nullable=True),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "agent_mode",
            postgresql.ENUM(
                "SOLO", "ASSISTED", "WRAP_UP", name="agent_mode", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'SOLO'"),
        ),
        sa.Column(
            "metadata_version", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "recording_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "transcript_exported_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_name"),
    )
    op.create_index(
        "ux_sessions_active_join_code",
        "sessions",
        ["join_code"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending','active') AND join_code IS NOT NULL"),
    )
    op.create_index(
        "ix_sessions_state_created", "sessions", ["state", "created_at"]
    )

    op.create_table(
        "session_participants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "role",
            postgresql.ENUM(
                "caller", "support", "agent", name="participant_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "role"),
    )

    op.create_table(
        "recordings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("egress_id", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "track_video",
                "track_audio",
                "room_composite",
                name="recording_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            postgresql.ENUM(
                "caller", "support", "agent", name="participant_role", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("track_sid", sa.Text(), nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                "starting",
                "active",
                "ending",
                "complete",
                "failed",
                "aborted",
                name="recording_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("gcs_uri", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("egress_id"),
    )
    op.create_index("ix_recordings_session", "recordings", ["session_id"])

    op.create_table(
        "transcript_utterances",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_utterance_id", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "caller", "support", "agent", name="participant_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(
                "agent_stt",
                "support_stt",
                "agent_llm",
                name="utterance_source",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "agent_mode",
            postgresql.ENUM(
                "SOLO", "ASSISTED", "WRAP_UP", name="agent_mode", create_type=False
            ),
            nullable=True,
        ),
        sa.Column("ai_enabled", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_utterance_id"),
    )
    op.create_index(
        "ix_utterance_session_time",
        "transcript_utterances",
        ["session_id", "start_ms"],
    )

    op.create_table(
        "agent_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "consent_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("consent_text_version", sa.Text(), nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "data_purges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("objects_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "utterances_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("data_purges")
    op.drop_table("consent_events")
    op.drop_table("agent_events")
    op.drop_table("transcript_utterances")
    op.drop_table("recordings")
    op.drop_table("session_participants")
    op.drop_table("sessions")

    op.execute("DROP TYPE IF EXISTS utterance_source")
    op.execute("DROP TYPE IF EXISTS recording_state")
    op.execute("DROP TYPE IF EXISTS recording_kind")
    op.execute("DROP TYPE IF EXISTS agent_mode")
    op.execute("DROP TYPE IF EXISTS participant_role")
    op.execute("DROP TYPE IF EXISTS session_state")
