"""Egress orchestration. Full rationale in docs/06-recording-transcripts.md.

Five egresses per session:
    1-4  Track Egress, one per human track  -> the four separate files (FR-5.1)
    5    Room Composite                     -> one MP4 for human review (FR-5.2)

Track egress is started from the ``track_published`` webhook, because a track
SID does not exist until the track is published and ``TrackEgressRequest``
requires one. There is no way to pre-arm this at room creation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from livekit import api
from sqlalchemy import select

from ..config import get_settings
from ..db import AsyncSessionLocal
from ..models import ParticipantRole, Recording, RecordingKind, RecordingState

if TYPE_CHECKING:
    from ..models import Session

VIDEO_EXT = "mp4"  # H.264 is forced client-side; see docs/06 ADR
AUDIO_EXT = "ogg"  # Opus

logger = logging.getLogger(__name__)
settings = get_settings()


def media_path(session_id: uuid.UUID, role: ParticipantRole, media: str, track_sid: str) -> str:
    ext = VIDEO_EXT if media == "video" else AUDIO_EXT
    return f"sessions/{session_id}/media/{role.value}-{media}-{track_sid}.{ext}"


def composite_path(session_id: uuid.UUID) -> str:
    return f"sessions/{session_id}/media/composite.mp4"


async def start_track_egress(
    lkapi: api.LiveKitAPI,
    session: Session,
    role: ParticipantRole,
    track_sid: str,
    is_video: bool,
) -> Recording | None:
    """Start one Track Egress and insert a ``recordings`` row.

    Idempotent: key on (session_id, track_sid). LiveKit retries webhooks, and a
    duplicate ``track_published`` must not start a second egress.

    Skips: the agent's own audio track, and any session with
    ``recording_enabled=False``.
    """
    if role == ParticipantRole.AGENT or not session.recording_enabled:
        return None

    media = "video" if is_video else "audio"
    kind = RecordingKind.TRACK_VIDEO if is_video else RecordingKind.TRACK_AUDIO
    filepath = media_path(session.id, role, media, track_sid)

    async with AsyncSessionLocal() as db:
        existing_result = await db.execute(
            select(Recording).where(
                Recording.session_id == session.id,
                Recording.track_sid == track_sid,
            )
        )
        existing: Recording | None = existing_result.scalar_one_or_none()
        if existing is not None:
            return existing

        info = await lkapi.egress.start_track_egress(
            api.TrackEgressRequest(
                room_name=session.room_name,
                track_id=track_sid,
                file=api.DirectFileOutput(
                    filepath=filepath,
                    gcp=api.GCPUpload(
                        credentials=settings.gcp_credentials_json,
                        bucket=settings.gcs_bucket,
                    ),
                ),
            )
        )

        recording = Recording(
            session_id=session.id,
            egress_id=info.egress_id,
            kind=kind,
            role=role,
            track_sid=track_sid,
            state=RecordingState.STARTING,
        )
        db.add(recording)
        await db.commit()
        return recording


async def start_room_composite_egress(
    lkapi: api.LiveKitAPI,
    session: Session,
) -> Recording | None:
    """Start the room composite egress once both humans have published."""
    async with AsyncSessionLocal() as db:
        existing_result = await db.execute(
            select(Recording).where(
                Recording.session_id == session.id,
                Recording.kind == RecordingKind.ROOM_COMPOSITE,
            )
        )
        existing: Recording | None = existing_result.scalar_one_or_none()
        if existing is not None:
            return existing

        info = await lkapi.egress.start_room_composite_egress(
            api.RoomCompositeEgressRequest(
                room_name=session.room_name,
                layout="grid",
                audio_only=False,
                file_outputs=[
                    api.EncodedFileOutput(
                        file_type=api.EncodedFileType.MP4,
                        filepath=composite_path(session.id),
                        gcp=api.GCPUpload(
                            credentials=settings.gcp_credentials_json,
                            bucket=settings.gcs_bucket,
                        ),
                    )
                ],
            )
        )

        recording = Recording(
            session_id=session.id,
            egress_id=info.egress_id,
            kind=RecordingKind.ROOM_COMPOSITE,
            role=None,
            track_sid=None,
            state=RecordingState.STARTING,
        )
        db.add(recording)
        await db.commit()
        return recording


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


async def on_egress_ended(egress_info: api.EgressInfo) -> None:
    """Mirror terminal state from ``EgressInfo`` into ``recordings``.

    The webhook handler in ``routers/webhooks.py`` already does this inline; this
    helper exists for any other code path that needs the same mapping.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Recording).where(Recording.egress_id == egress_info.egress_id)
        )
        recording: Recording | None = result.scalar_one_or_none()
        if recording is None:
            logger.warning("egress_ended for unknown egress_id=%s", egress_info.egress_id)
            return

        recording.state = _EGRESS_STATUS_MAP.get(
            egress_info.status, RecordingState.STARTING
        )
        if egress_info.started_at:
            recording.started_at = datetime.fromtimestamp(
                egress_info.started_at, tz=UTC
            )
        if egress_info.ended_at:
            recording.ended_at = datetime.fromtimestamp(
                egress_info.ended_at, tz=UTC
            )
        if egress_info.error:
            recording.error = egress_info.error

        if egress_info.file_results:
            file_result = egress_info.file_results[0]
            recording.gcs_uri = f"gs://{settings.gcs_bucket}/{file_result.filename}"
            recording.size_bytes = file_result.size if file_result.size else None
            recording.duration_ms = (
                int(file_result.duration * 1000) if file_result.duration else None
            )
            recording.mime_type = _mime_type_for_filename(file_result.filename)

        await db.commit()
