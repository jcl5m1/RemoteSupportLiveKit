"""Timestamped interaction log for open calls, stored next to the recordings.

Clients report what *they* send or do (chat text incl. ``/show``, pointer
clicks, mute/camera/view controls, leaving); the backend adds its own events
(join, recording start/stop, call end). Each event keeps two timestamps:

* ``ts`` (``server_ts`` column) -- when it happened, on the *server* clock
  (same UTC clock family as the LiveKit egress recordings, so align video with
  it). For client events: ``client_ts + (received - batch sent_at)``, i.e. the
  sender's timestamp shifted by the per-batch clock offset, which cancels both
  the batching delay and clock skew (leaving one-way network latency).
* ``received_ts`` -- when the backend got it.
* ``client_ts`` -- the sender's raw clock (ms since epoch).

Events are exported as JSON lines to
``calls/<call_id>/<first event UTC>_<room_sid>_events.jsonl`` in GCS_BUCKET:
debounced after new events, and immediately on recording stop / call end.
One file per room instance (LiveKit room SID), rewritten in full each export.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..config import get_settings
from ..db import AsyncSessionLocal
from ..models import CallEvent

logger = logging.getLogger(__name__)
settings = get_settings()

EXPORT_DEBOUNCE_SECONDS = 30.0
_SID_TTL_SECONDS = 30.0
_sid_cache: dict[str, tuple[str | None, float]] = {}
_export_tasks: dict[tuple[str, str | None], asyncio.Task[None]] = {}


async def room_sid(lkapi: Any, room_name: str) -> str | None:
    """LiveKit SID of the live room instance (cached briefly)."""
    from . import call_recording  # local import: avoid a cycle

    hit = _sid_cache.get(room_name)
    if hit and time.monotonic() - hit[1] < _SID_TTL_SECONDS:
        return hit[0]
    try:
        room = await call_recording.get_room(lkapi, room_name)
        sid = getattr(room, "sid", None) or None
    except Exception:  # noqa: BLE001
        sid = None
    _sid_cache[room_name] = (sid, time.monotonic())
    return sid


def remember_sid(room_name: str, sid: str | None) -> None:
    if sid:
        _sid_cache[room_name] = (sid, time.monotonic())


MAX_CLIENT_AGE_MS = 10 * 60 * 1000  # ignore implausible sender timestamps


async def record(
    call_id: str,
    sid: str | None,
    identity: str | None,
    name: str | None,
    events: list[dict],
    sent_at: int | None = None,
) -> int:
    """Insert events (``{type, data, ts?, id?}``); duplicate ``id``s are ignored.

    ``sent_at`` is the sender's clock when the batch left; with each event's
    ``ts`` it maps the event onto the server clock (see module doc).
    """
    if not events:
        return 0
    received = datetime.now(UTC)
    received_ms = int(received.timestamp() * 1000)
    offset = received_ms - sent_at if sent_at else None

    def happened(e: dict) -> datetime:
        at = e.get("at")
        if isinstance(at, datetime):
            return at
        ts = e.get("ts")
        if offset is None or not ts:
            return received
        at_ms = ts + offset
        if not received_ms - MAX_CLIENT_AGE_MS <= at_ms <= received_ms:
            return received
        return datetime.fromtimestamp(at_ms / 1000, UTC)

    rows = [
        {
            "call_id": call_id,
            "room_sid": sid,
            "event_id": e.get("id"),
            "server_ts": happened(e),
            "received_ts": received,
            "client_ts": e.get("ts"),
            "identity": identity,
            "name": name,
            "type": e["type"],
            "data": e.get("data") or {},
        }
        for e in events
    ]
    stmt = (
        insert(CallEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(CallEvent.id)
    )
    async with AsyncSessionLocal() as db:
        inserted = (await db.execute(stmt)).scalars().all()
        await db.commit()
    return len(inserted)


async def log_server_event(
    call_id: str, sid: str | None, type_: str, identity: str | None = None,
    name: str | None = None, data: dict | None = None, at: datetime | None = None,
) -> None:
    """Server-side event. ``at``: when it took effect (e.g. request arrival),
    rather than when the (possibly slow) work behind it finished."""
    event: dict = {"type": type_, "data": data or {}}
    if at is not None:
        event["at"] = at
    try:
        await record(call_id, sid, identity, name, [event])
    except Exception:  # noqa: BLE001 -- logging must never break the call
        logger.exception("call event %s not recorded for %s", type_, call_id)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


async def export(gcs_client: Any, call_id: str, sid: str | None) -> str | None:
    """Write this room instance's events as JSON lines to GCS; returns the path."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CallEvent)
            .where(CallEvent.call_id == call_id)
            .where(CallEvent.room_sid == sid if sid else CallEvent.room_sid.is_(None))
            .order_by(CallEvent.server_ts, CallEvent.id)
        )
        events = list(result.scalars())
    if not events:
        return None
    lines = [
        json.dumps(
            {
                "ts": e.server_ts.astimezone(UTC).isoformat(timespec="milliseconds"),
                "received_ts": e.received_ts.astimezone(UTC).isoformat(timespec="milliseconds"),
                "client_ts": (
                    datetime.fromtimestamp(e.client_ts / 1000, UTC).isoformat(
                        timespec="milliseconds"
                    )
                    if e.client_ts
                    else None
                ),
                "type": e.type,
                "identity": e.identity,
                "name": e.name,
                "data": e.data,
            },
            ensure_ascii=False,
        )
        for e in events
    ]
    path = f"calls/{call_id}/{_stamp(events[0].server_ts)}_{sid or 'room'}_events.jsonl"

    def upload() -> None:
        blob = gcs_client.bucket(settings.gcs_bucket).blob(path)
        blob.upload_from_string("\n".join(lines) + "\n", content_type="application/x-ndjson")

    await asyncio.to_thread(upload)
    return path


def schedule_export(gcs_client: Any, call_id: str, sid: str | None) -> None:
    """Export once activity settles (one pending export per room instance)."""
    key = (call_id, sid)
    task = _export_tasks.get(key)
    if task and not task.done():
        return

    async def later() -> None:
        try:
            await asyncio.sleep(EXPORT_DEBOUNCE_SECONDS)
            await export(gcs_client, call_id, sid)
        except Exception:  # noqa: BLE001
            logger.exception("call event export failed for %s", call_id)
        finally:
            _export_tasks.pop(key, None)

    _export_tasks[key] = asyncio.create_task(later())


async def export_now(gcs_client: Any, call_id: str, sid: str | None) -> str | None:
    task = _export_tasks.pop((call_id, sid), None)
    if task and not task.done():
        task.cancel()
    try:
        return await export(gcs_client, call_id, sid)
    except Exception:  # noqa: BLE001
        logger.exception("call event export failed for %s", call_id)
        return None
