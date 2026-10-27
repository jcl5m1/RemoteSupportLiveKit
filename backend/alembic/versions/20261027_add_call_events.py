"""add_call_events: timestamped interaction log for open calls"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261027_0003_call_events"
down_revision: str | None = "e1916822e0ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("room_sid", sa.Text(), nullable=True),
        sa.Column("event_id", sa.Text(), nullable=True),
        sa.Column(
            "server_ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "received_ts", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("client_ts", sa.BigInteger(), nullable=True),
        sa.Column("identity", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_call_events_call_time", "call_events", ["call_id", "room_sid", "server_ts"]
    )
    op.create_index("ux_call_events_event_id", "call_events", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_call_events_event_id", table_name="call_events")
    op.drop_index("ix_call_events_call_time", table_name="call_events")
    op.drop_table("call_events")
