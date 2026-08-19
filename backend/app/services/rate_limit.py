"""Postgres-backed sliding-window rate limiter.

The window is fixed to the current time floor; this is good enough for the
limits in docs/08-security-compliance.md and avoids a Redis dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from ..db import AsyncSessionLocal
from ..models import RateLimit


async def check_rate_limit(
    *,
    key: str,
    limit: int,
    window_seconds: int,
    db=None,
) -> bool:
    """Return True if the request is allowed under the limit.

    The counter is created lazily and incremented atomically. The ``db``
    parameter lets callers that already hold a session reuse it; otherwise a
    short-lived session is opened.
    """
    now = datetime.now(UTC)
    epoch_seconds = int(now.timestamp())
    window_start_epoch = (epoch_seconds // window_seconds) * window_seconds
    window_start = datetime.fromtimestamp(window_start_epoch, tz=UTC)

    stmt = (
        insert(RateLimit)
        .values(key=key, window_start=window_start, count=1)
        .on_conflict_do_update(
            index_elements=["key", "window_start"],
            set_=dict(count=RateLimit.count + 1),
        )
        .returning(RateLimit.count)
    )

    if db is not None:
        result = await db.execute(stmt)
        count = result.scalar() or 0
        return count <= limit

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        count = result.scalar() or 0
        await db.commit()
        return count <= limit


def client_ip(request) -> str:
    """Best-guess client IP, honouring a single trusted proxy header."""
    forwarded = request.headers.get("x-forwarded-for")
    if isinstance(forwarded, str):
        return forwarded.split(",")[0].strip()
    if request.client:
        return str(request.client.host)
    return "unknown"
