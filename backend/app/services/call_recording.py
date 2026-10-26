"""On-demand per-participant recording for open calls.

Any participant can toggle recording. While it is on, every participant who
publishes audio or video gets one Participant Egress: their camera and mic
muxed into a single MP4 at ``calls/<call_id>/<utc>_<identity>.mp4`` in
GCS_BUCKET. One egress per person (not per track) halves usage of the LiveKit
project's concurrent-egress cap. The egress follows the participant's tracks,
so a camera turned on later is captured in the same file.

State lives in LiveKit room metadata (``{"recording": bool, ...}``) so every
client sees it via ``RoomMetadataChanged`` and it survives a backend restart.
Participants who start publishing after recording starts (late joiners) are
picked up by ``reconcile``, which runs from a per-room background loop and
whenever a client nudges ``/recording/sync`` after publishing. A per-room lock
keeps concurrent reconciles from double-starting an egress for one person; this
assumes a single backend process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from livekit import api

from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

RECONCILE_INTERVAL_SECONDS = 3.0
_LIVE_EGRESS = (api.EgressStatus.EGRESS_STARTING, api.EgressStatus.EGRESS_ACTIVE)

_locks: dict[str, asyncio.Lock] = {}
_loops: dict[str, asyncio.Task[None]] = {}


def _lock(room_name: str) -> asyncio.Lock:
    return _locks.setdefault(room_name, asyncio.Lock())


def participant_filepath(call_id: str, identity: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"calls/{call_id}/{stamp}_{identity}.mp4"


async def get_state(lkapi: api.LiveKitAPI, room_name: str) -> dict:
    """Recording state from room metadata; ``{}`` if the room does not exist."""
    resp = await lkapi.room.list_rooms(api.ListRoomsRequest(names=[room_name]))
    for room in resp.rooms:
        if room.name == room_name and room.metadata:
            try:
                state = json.loads(room.metadata)
            except ValueError:
                return {}
            return state if isinstance(state, dict) else {}
    return {}


async def _set_state(lkapi: api.LiveKitAPI, room_name: str, state: dict) -> None:
    await lkapi.room.update_room_metadata(
        api.UpdateRoomMetadataRequest(room=room_name, metadata=json.dumps(state))
    )


async def _live_egresses(lkapi: api.LiveKitAPI, room_name: str) -> list[api.EgressInfo]:
    resp = await lkapi.egress.list_egress(
        api.ListEgressRequest(room_name=room_name, active=True)
    )
    return [e for e in resp.items if e.status in _LIVE_EGRESS]


def _has_media(participant: api.ParticipantInfo) -> bool:
    return any(
        t.type in (api.TrackType.AUDIO, api.TrackType.VIDEO) for t in participant.tracks
    )


async def _reconcile_locked(
    lkapi: api.LiveKitAPI, call_id: str, room_name: str, state: dict
) -> int:
    """Start an egress for every publishing participant not yet recorded; publish
    the count refused (e.g. the LiveKit plan's concurrent-egress cap) as
    ``skipped``."""
    recorded = {
        e.participant.identity
        for e in await _live_egresses(lkapi, room_name)
        if e.HasField("participant")
    }
    participants = await lkapi.room.list_participants(
        api.ListParticipantsRequest(room=room_name)
    )
    started = skipped = 0
    for participant in participants.participants:
        if participant.identity in recorded or not _has_media(participant):
            continue
        filepath = participant_filepath(call_id, participant.identity)
        try:
            await lkapi.egress.start_participant_egress(
                api.ParticipantEgressRequest(
                    room_name=room_name,
                    identity=participant.identity,
                    screen_share=False,
                    file_outputs=[
                        api.EncodedFileOutput(
                            file_type=api.EncodedFileType.MP4,
                            filepath=filepath,
                            gcp=api.GCPUpload(
                                credentials=settings.gcp_credentials_json,
                                bucket=settings.gcs_bucket,
                            ),
                        )
                    ],
                )
            )
            started += 1
            logger.info("call recording: started %s", filepath)
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            if getattr(exc, "status", None) == 429:
                logger.warning(
                    "call recording: egress limit reached, %s not recorded",
                    participant.identity,
                )
            else:
                logger.exception(
                    "call recording: participant egress failed for %s",
                    participant.identity,
                )
    if state.get("skipped", 0) != skipped:
        state["skipped"] = skipped
        await _set_state(lkapi, room_name, state)
    return started


async def reconcile(lkapi: api.LiveKitAPI, call_id: str, room_name: str) -> int:
    """Start egress for every publishing participant not already recorded.

    No-op unless room metadata says recording is on. Returns egresses started.
    """
    async with _lock(room_name):
        state = await get_state(lkapi, room_name)
        if not state.get("recording"):
            return 0
        return await _reconcile_locked(lkapi, call_id, room_name, state)


async def _loop(lkapi: api.LiveKitAPI, call_id: str, room_name: str) -> None:
    try:
        while True:
            async with _lock(room_name):
                state = await get_state(lkapi, room_name)
                if not state.get("recording"):
                    return
                await _reconcile_locked(lkapi, call_id, room_name, state)
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("call recording loop crashed for %s", room_name)
    finally:
        _loops.pop(room_name, None)


def ensure_loop(lkapi: api.LiveKitAPI, call_id: str, room_name: str) -> None:
    task = _loops.get(room_name)
    if task is None or task.done():
        _loops[room_name] = asyncio.create_task(_loop(lkapi, call_id, room_name))


async def start(
    lkapi: api.LiveKitAPI, call_id: str, room_name: str, started_by: str
) -> dict:
    async with _lock(room_name):
        state = await get_state(lkapi, room_name)
        if not state.get("recording"):
            state = {
                "recording": True,
                "started_by": started_by,
                "started_at": datetime.now(UTC).isoformat(),
            }
            await _set_state(lkapi, room_name, state)
        await _reconcile_locked(lkapi, call_id, room_name, state)
    ensure_loop(lkapi, call_id, room_name)
    return state


async def end_call(lkapi: api.LiveKitAPI, room_name: str, ended_by: str) -> None:
    """End the call for everyone: finalize recordings, then delete the room.

    ``ended_by`` is published in room metadata first so clients can say who
    ended it when they are disconnected. A room that is already gone is fine.
    """
    try:
        await stop(lkapi, room_name, ended_by)
        await _set_state(lkapi, room_name, {"recording": False, "ended_by": ended_by})
        # Let the metadata update reach clients before the room disappears.
        await asyncio.sleep(0.3)
        await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) != 404:
            raise
    _locks.pop(room_name, None)


async def stop(lkapi: api.LiveKitAPI, room_name: str, stopped_by: str) -> dict:
    task = _loops.pop(room_name, None)
    if task is not None:
        task.cancel()
    async with _lock(room_name):
        state = {"recording": False, "stopped_by": stopped_by}
        await _set_state(lkapi, room_name, state)
        for egress in await _live_egresses(lkapi, room_name):
            try:
                await lkapi.egress.stop_egress(
                    api.StopEgressRequest(egress_id=egress.egress_id)
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "call recording: stop_egress failed for %s", egress.egress_id
                )
    return state
