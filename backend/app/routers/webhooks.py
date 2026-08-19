"""LiveKit webhook receiver.

Two non-negotiables:
  * Verify the signature before parsing the body.
  * Return 200 fast and make every handler idempotent -- LiveKit retries, and a
    duplicate ``track_published`` must not start a second egress.

Event -> action table is in docs/04-api-contract.md § webhooks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from livekit import api
from livekit.api.access_token import TokenVerifier
from sqlalchemy import select

from .. import metrics
from ..config import get_settings
from ..db import AsyncSessionLocal
from ..models import (
    AgentEvent,
    AgentMode,
    ParticipantRole,
    Recording,
    RecordingKind,
    RecordingState,
    Session,
    SessionParticipant,
    SessionState,
)
from ..services import dispatch, egress, transcripts

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.post("/livekit")
async def livekit_webhook(request: Request) -> Response:
    """Verify, dispatch, and always return 200."""
    body_bytes = await request.body()
    auth_header = request.headers.get("Authorization", "")

    receiver = api.WebhookReceiver(
        TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    try:
        event = receiver.receive(body_bytes.decode("utf-8"), auth_header)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LiveKit webhook verification failed: %s", exc)
        return Response(status_code=200)

    try:
        await _dispatch(request, event)
    except Exception:  # noqa: BLE001
        logger.exception("LiveKit webhook handler failed: event=%s", event.event)

    return Response(status_code=200)


async def _dispatch(request: Request, event) -> None:
    handlers = {
        "room_started": _handle_room_started,
        "participant_joined": _handle_participant_joined,
        "track_published": _handle_track_published,
        "track_unpublished": _handle_track_unpublished,
        "participant_left": _handle_participant_left,
        "egress_started": _handle_egress,
        "egress_updated": _handle_egress,
        "egress_ended": _handle_egress,
        "room_finished": _handle_room_finished,
    }
    handler = handlers.get(event.event)
    if handler is None:
        return
    await handler(request, event)


async def _session_by_room(room_name: str) -> Session | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Session).where(Session.room_name == room_name))
        return result.scalar_one_or_none()


def _role_for_identity(session: Session, identity: str) -> ParticipantRole | None:
    if identity == session.caller_identity:
        return ParticipantRole.CALLER
    if identity == session.support_identity:
        return ParticipantRole.SUPPORT
    if identity == "agent":
        return ParticipantRole.AGENT
    return None


def _ts_to_datetime(value: int) -> datetime:
    """LiveKit timestamps are Unix seconds; tolerate milliseconds."""
    if value > 1_000_000_000_000:
        value = value // 1000
    return datetime.fromtimestamp(value, tz=UTC)


async def _handle_room_started(request: Request, event) -> None:
    room_name = event.room.name
    session = await _session_by_room(room_name)
    if session is None:
        return

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session.id)
        if session is None:
            return
        if session.state != SessionState.ACTIVE:
            session.state = SessionState.ACTIVE
            await db.commit()


async def _handle_participant_joined(request: Request, event) -> None:
    room_name = event.room.name
    identity = event.participant.identity
    display_name = event.participant.name or None

    session = await _session_by_room(room_name)
    if session is None:
        return

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session.id)
        if session is None:
            return

        role = _role_for_identity(session, identity)
        if role is None:
            return

        result = await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.role == role,
            )
        )
        participant = result.scalar_one_or_none()
        now = datetime.now(UTC)
        if participant is None:
            participant = SessionParticipant(
                session_id=session.id,
                role=role,
                identity=identity,
                display_name=display_name,
                joined_at=now,
            )
            db.add(participant)
        else:
            participant.joined_at = now
            participant.left_at = None
            if display_name:
                participant.display_name = display_name

        if session.started_at is None:
            session.started_at = now

        changed_mode = False
        if role == ParticipantRole.SUPPORT and session.agent_mode == AgentMode.SOLO:
            old_mode = session.agent_mode
            session.agent_mode = AgentMode.ASSISTED
            session.metadata_version += 1
            db.add(
                AgentEvent(
                    session_id=session.id,
                    event_type="mode_changed",
                    actor="system",
                    payload={"old": old_mode.value, "new": AgentMode.ASSISTED.value},
                )
            )
            changed_mode = True

        await db.commit()

        if changed_mode:
            try:
                await dispatch.update_room_metadata(request.app.state.lkapi, session)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to update room metadata on support join")


async def _handle_track_published(request: Request, event) -> None:
    room_name = event.room.name
    identity = event.participant.identity
    track = event.track

    session = await _session_by_room(room_name)
    if session is None:
        return

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session.id)
        if session is None:
            return

        if not session.recording_enabled:
            return

        role = _role_for_identity(session, identity)
        if role == ParticipantRole.AGENT or role is None:
            return

        track_type = track.type
        if track_type == api.TrackType.DATA:
            return

        is_video = track_type == api.TrackType.VIDEO
        try:
            await egress.start_track_egress(
                request.app.state.lkapi, session, role, track.sid, is_video
            )
        except Exception:  # noqa: BLE001
            logger.exception("start_track_egress failed for %s", track.sid)

        # Start room composite once both humans have published at least one track.
        composite_result = await db.execute(
            select(Recording).where(
                Recording.session_id == session.id,
                Recording.kind == RecordingKind.ROOM_COMPOSITE,
            )
        )
        if composite_result.scalar_one_or_none() is not None:
            return

        human_roles = {
            row[0]
            for row in (
                await db.execute(
                    select(Recording.role)
                    .where(Recording.session_id == session.id)
                    .where(
                        Recording.role.in_(
                            [ParticipantRole.CALLER, ParticipantRole.SUPPORT]
                        )
                    )
                    .distinct()
                )
            ).all()
        }
        if ParticipantRole.CALLER in human_roles and ParticipantRole.SUPPORT in human_roles:
            try:
                await egress.start_room_composite_egress(request.app.state.lkapi, session)
            except Exception:  # noqa: BLE001
                logger.exception("start_room_composite_egress failed")


async def _handle_track_unpublished(request: Request, event) -> None:
    return


async def _handle_participant_left(request: Request, event) -> None:
    room_name = event.room.name
    identity = event.participant.identity

    session = await _session_by_room(room_name)
    if session is None:
        return

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session.id)
        if session is None:
            return

        role = _role_for_identity(session, identity)
        if role is None:
            return

        result = await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.role == role,
            )
        )
        participant = result.scalar_one_or_none()
        if participant is not None:
            participant.left_at = datetime.now(UTC)

        is_human = role in (ParticipantRole.CALLER, ParticipantRole.SUPPORT)
        if is_human and session.agent_mode != AgentMode.WRAP_UP:
            old_mode = session.agent_mode
            session.agent_mode = AgentMode.WRAP_UP
            session.metadata_version += 1
            db.add(
                AgentEvent(
                    session_id=session.id,
                    event_type="mode_changed",
                    actor="system",
                    payload={"old": old_mode.value, "new": AgentMode.WRAP_UP.value},
                )
            )
            await db.commit()
            try:
                await dispatch.update_room_metadata(request.app.state.lkapi, session)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to update room metadata on participant left")
        else:
            await db.commit()


_EGRESS_STATUS_MAP = {
    api.EgressStatus.EGRESS_STARTING: RecordingState.STARTING,
    api.EgressStatus.EGRESS_ACTIVE: RecordingState.ACTIVE,
    api.EgressStatus.EGRESS_ENDING: RecordingState.ENDING,
    api.EgressStatus.EGRESS_COMPLETE: RecordingState.COMPLETE,
    api.EgressStatus.EGRESS_FAILED: RecordingState.FAILED,
    api.EgressStatus.EGRESS_ABORTED: RecordingState.ABORTED,
    api.EgressStatus.EGRESS_LIMIT_REACHED: RecordingState.FAILED,
}


def _mime_type_for_filename(filename: str) -> str | None:
    if filename.endswith(".mp4"):
        return "video/mp4"
    if filename.endswith(".ogg"):
        return "audio/ogg"
    if filename.endswith(".ivf"):
        return "video/x-ivf"
    return None


def _role_from_filename(filename: str) -> ParticipantRole | None:
    parts = filename.split("/")
    if not parts:
        return None
    name = parts[-1]
    if name.startswith("caller-"):
        return ParticipantRole.CALLER
    if name.startswith("support-"):
        return ParticipantRole.SUPPORT
    if name.startswith("agent-"):
        return ParticipantRole.AGENT
    return None


def _kind_from_filename(filename: str) -> RecordingKind | None:
    if "-video-" in filename:
        return RecordingKind.TRACK_VIDEO
    if "-audio-" in filename:
        return RecordingKind.TRACK_AUDIO
    if "composite" in filename:
        return RecordingKind.ROOM_COMPOSITE
    return None


async def _handle_egress(request: Request, event) -> None:
    info = event.egress_info
    if info is None:
        return

    async with AsyncSessionLocal() as db:
        # Resolve the session first; egress events without a known room are dropped.
        session_result = await db.execute(
            select(Session).where(Session.room_name == info.room_name)
        )
        session = session_result.scalar_one_or_none()
        if session is None:
            return

        result = await db.execute(
            select(Recording).where(Recording.egress_id == info.egress_id)
        )
        recording = result.scalar_one_or_none()

        kind: RecordingKind | None = None
        role: ParticipantRole | None = None
        track_sid: str | None = None

        if info.HasField("track"):
            kind = RecordingKind.TRACK_AUDIO
            track_sid = info.track.track_id
            if info.file_results:
                filename = info.file_results[0].filename
                kind = _kind_from_filename(filename) or kind
                role = _role_from_filename(filename)
        elif info.HasField("room_composite"):
            kind = RecordingKind.ROOM_COMPOSITE
            role = None
            track_sid = None
        else:
            return

        state = _EGRESS_STATUS_MAP.get(info.status, RecordingState.STARTING)

        if recording is None:
            recording = Recording(
                session_id=session.id,
                egress_id=info.egress_id,
                kind=kind or RecordingKind.TRACK_AUDIO,
                role=role,
                track_sid=track_sid,
                state=state,
            )
            db.add(recording)

        recording.state = state
        metrics.EGRESS_EVENTS.labels(
            kind=recording.kind.value, state=recording.state.value
        ).inc()
        if info.started_at:
            recording.started_at = _ts_to_datetime(info.started_at)
        if info.ended_at:
            recording.ended_at = _ts_to_datetime(info.ended_at)

        if info.error:
            recording.error = info.error

        if event.event == "egress_ended" and info.file_results:
            file_result = info.file_results[0]
            recording.gcs_uri = f"gs://{settings.gcs_bucket}/{file_result.filename}"
            recording.size_bytes = file_result.size if file_result.size else None
            recording.duration_ms = (
                int(file_result.duration * 1000) if file_result.duration else None
            )
            recording.mime_type = _mime_type_for_filename(file_result.filename)

        await db.commit()


async def _handle_room_finished(request: Request, event) -> None:
    room_name = event.room.name
    session = await _session_by_room(room_name)
    if session is None:
        return

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session.id)
        if session is None:
            return

        session.ended_at = datetime.now(UTC)
        session.state = SessionState.COMPLETED
        await db.commit()

        try:
            await transcripts.export_transcript(db, request.app.state.gcs_client, session)
        except Exception:  # noqa: BLE001
            logger.exception("Transcript export failed for session %s", session.id)
