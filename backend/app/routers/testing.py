"""Internal testing endpoints for the automated regression harness.

These endpoints are only mounted when ``settings.allow_test_endpoints`` is true.
They bypass production auth (Firebase) and must never be enabled on a
public-facing deployment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from pydantic import BaseModel
from sqlalchemy import select

from .. import schemas
from ..config import get_settings
from ..db import AsyncSessionLocal
from ..deps import require_service_key
from ..models import Recording, RecordingState, Session, SessionParticipant, SessionState
from ..services import dispatch, storage
from ..services.livekit_tokens import (
    ParticipantRole,
    build_token,
    make_support_identity,
)

router = APIRouter(prefix="/internal/test", tags=["internal-test"])
settings = get_settings()

TEST_SUPPORT_USER_ID = "test-support"


class SupportTokenRequest(BaseModel):
    session_id: uuid.UUID


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


@router.post("/support-token", response_model=schemas.LiveKitCredentials)
async def test_support_token(
    body: SupportTokenRequest,
    request: Request,
    _service: None = Depends(require_service_key),
):
    """Mint a support LiveKit token for the regression harness.

    This bypasses Firebase auth and assigns a synthetic support identity. It is
    meant for one test run per session.
    """
    lkapi: api.LiveKitAPI = request.app.state.lkapi

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, body.session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        if session.state not in (SessionState.PENDING, SessionState.ACTIVE):
            _error(
                "session_not_joinable",
                "Session is not joinable",
                status.HTTP_409_CONFLICT,
            )

        support_identity = make_support_identity(TEST_SUPPORT_USER_ID)
        if session.support_identity and session.support_identity != support_identity:
            _error(
                "role_occupied",
                "Support role is already occupied",
                status.HTTP_409_CONFLICT,
            )

        session.support_identity = support_identity
        session.support_user_id = TEST_SUPPORT_USER_ID

        # Create the room if the caller has not yet consented.
        if session.state == SessionState.PENDING:
            session.state = SessionState.ACTIVE
            await _start_room(lkapi, session)
        else:
            await dispatch.update_room_metadata(lkapi, session)

        # Upsert participant row. The unique index enforces one support per session.
        existing_support = await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.role == ParticipantRole.SUPPORT,
            )
        )
        if existing_support.scalar_one_or_none() is not None:
            _error(
                "role_occupied",
                "Support role is already occupied",
                status.HTTP_409_CONFLICT,
            )

        participant = SessionParticipant(
            session_id=session.id,
            role=ParticipantRole.SUPPORT,
            identity=support_identity,
            display_name="Test Support",
            joined_at=datetime.now(UTC),
        )
        db.add(participant)

        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.livekit_token_ttl_seconds
        )
        token = build_token(
            room_name=session.room_name,
            identity=support_identity,
            role=ParticipantRole.SUPPORT,
            display_name="Test Support",
            session_id=session.id,
            ttl_seconds=settings.livekit_token_ttl_seconds,
        )

        await db.commit()

        return schemas.LiveKitCredentials(
            ws_url=settings.livekit_url,
            token=token,
            room_name=session.room_name,
            identity=support_identity,
            expires_at=expires_at,
        )


@router.get("/{session_id}/recordings", response_model=schemas.RecordingsResponse)
async def test_list_recordings(
    session_id: uuid.UUID,
    request: Request,
    _service: None = Depends(require_service_key),
):
    """List recordings and transcript exports for the regression harness.

    Mirrors ``GET /v1/sessions/{id}/recordings`` but uses the service key so the
    harness does not need Firebase support auth.
    """
    gcs_client = request.app.state.gcs_client
    ttl = timedelta(seconds=settings.signed_url_ttl_seconds)
    expires_at = datetime.now(UTC) + ttl

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)

        result = await db.execute(
            select(Recording).where(Recording.session_id == session_id)
        )
        recordings = result.scalars().all()

        recording_infos: list[schemas.RecordingInfo] = []
        for rec in recordings:
            download_url: str | None = None
            url_expires_at: datetime | None = None
            if rec.state == RecordingState.COMPLETE and rec.gcs_uri:
                download_url = storage.make_signed_url(
                    gcs_client, rec.gcs_uri, ttl_seconds=settings.signed_url_ttl_seconds
                )
                url_expires_at = expires_at

            recording_infos.append(
                schemas.RecordingInfo(
                    kind=rec.kind,
                    role=rec.role,
                    state=rec.state,
                    mime_type=rec.mime_type,
                    duration_ms=rec.duration_ms,
                    size_bytes=rec.size_bytes,
                    gcs_uri=rec.gcs_uri,
                    download_url=download_url,
                    url_expires_at=url_expires_at,
                )
            )

        transcript_urls = _transcript_export_urls(gcs_client, session_id, ttl)

    return schemas.RecordingsResponse(
        recordings=recording_infos,
        transcript=transcript_urls,
    )


def _transcript_export_urls(
    gcs_client,
    session_id: uuid.UUID,
    ttl: timedelta,
) -> schemas.TranscriptExportUrls | None:
    """Return signed URLs for transcript exports, only when all three exist."""
    bucket = gcs_client.bucket(settings.gcs_bucket)
    paths = {
        "jsonl": f"sessions/{session_id}/transcript/transcript.jsonl",
        "vtt": f"sessions/{session_id}/transcript/transcript.vtt",
        "txt": f"sessions/{session_id}/transcript/transcript.txt",
    }

    signed: dict[str, str] = {}
    for key, path in paths.items():
        blob = bucket.blob(path)
        if blob.exists():
            signed[key] = storage.make_signed_url(
                gcs_client,
                storage.gcs_uri(path),
                ttl_seconds=int(ttl.total_seconds()),
            )

    if len(signed) == len(paths):
        return schemas.TranscriptExportUrls(
            jsonl_url=signed["jsonl"],
            vtt_url=signed["vtt"],
            txt_url=signed["txt"],
        )
    return None


async def _start_room(lkapi: api.LiveKitAPI, session: Session) -> None:
    """Create the LiveKit room, dispatch the agent, and publish metadata."""
    await lkapi.room.create_room(
        api.CreateRoomRequest(
            name=session.room_name,
            empty_timeout=settings.room_empty_timeout_seconds,
            departure_timeout=settings.room_departure_timeout_seconds,
        )
    )
    await dispatch.dispatch_agent(lkapi, session)
    await dispatch.update_room_metadata(lkapi, session)
