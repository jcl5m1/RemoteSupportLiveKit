"""Session lifecycle endpoints. Contract: docs/04-api-contract.md."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from sqlalchemy import delete, func, select

from .. import metrics, schemas
from ..config import get_settings
from ..db import AsyncSessionLocal
from ..deps import (
    CallerTokenPayload,
    SupportTokenPayload,
    issue_caller_token,
    require_admin,
    require_caller_session,
    require_support_user,
)
from ..models import ConsentEvent, DataPurge, Recording, Session, SessionParticipant, SessionState
from ..services import dispatch, storage
from ..services.livekit_tokens import (
    ParticipantRole,
    build_token,
    make_caller_identity,
    make_support_identity,
    room_name_for,
)
from ..services.rate_limit import check_rate_limit, client_ip
from ..services.room_codes import claim_code, normalize_code

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
settings = get_settings()

CONSENT_TEXT = """\
This call, including video and audio from both participants, will be recorded \
and transcribed to help resolve your support request. By continuing, you consent \
to this recording and transcription. The recording will be stored securely and \
retained according to our privacy policy."""


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


@router.post("", status_code=201, response_model=schemas.CreateSessionResponse)
async def create_session(body: schemas.CreateSessionRequest, request: Request):
    """Create a session. Auth: none. Rate limit: 5/min/IP, 20/hour/device."""
    ip = client_ip(request)
    if not await check_rate_limit(
        key=f"create_session:ip:{ip}", limit=5, window_seconds=60
    ):
        _error("rate_limit", "Too many sessions from this IP", status.HTTP_429_TOO_MANY_REQUESTS)
    if not await check_rate_limit(
        key=f"create_session:device:{body.device_id}", limit=20, window_seconds=3600
    ):
        _error(
            "rate_limit",
            "Too many sessions from this device",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    async with AsyncSessionLocal() as db:
        session = Session(
            room_name=room_name_for(uuid.uuid4()),
            state=SessionState.PENDING,
        )
        db.add(session)
        await db.flush()

        caller_identity = make_caller_identity()
        session.caller_identity = caller_identity
        code = await claim_code(db, session)
        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.join_code_ttl_seconds
        )
        session.join_code_expires_at = expires_at

        await db.commit()

        token = issue_caller_token(session.id, caller_identity)
        universal = f"https://{settings.app_link_host}/j/{code}"

        response = schemas.CreateSessionResponse(
            session_id=session.id,
            join_code=code,
            join_code_expires_at=expires_at,
            deep_link=f"{settings.deep_link_scheme}://join?code={code}",
            universal_link=universal,
            qr_payload=universal,
            caller_session_token=token,
            consent_required=True,
            consent_text_version=settings.consent_text_version,
            consent_text=CONSENT_TEXT,
        )
        metrics.SESSIONS_CREATED.labels(
            recording_enabled=str(session.recording_enabled)
        ).inc()
        return response


@router.post("/{session_id}/consent", response_model=schemas.ConsentResponse)
async def record_consent(
    session_id: uuid.UUID,
    body: schemas.ConsentRequest,
    request: Request,
    caller: CallerTokenPayload = Depends(require_caller_session),
):
    """Record the consent decision and, on acceptance, actually start the call."""
    if caller.session_id != session_id:
        _error("forbidden", "Token does not authorize this session", status.HTTP_403_FORBIDDEN)

    if not await check_rate_limit(
        key=f"consent:session:{session_id}", limit=5, window_seconds=60
    ):
        _error("rate_limit", "Too many consent attempts", status.HTTP_429_TOO_MANY_REQUESTS)

    lkapi: api.LiveKitAPI = request.app.state.lkapi

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        # Idempotency: return existing decision.
        existing = await db.execute(
            select(ConsentEvent).where(ConsentEvent.session_id == session_id)
        )
        if existing.scalar_one_or_none() is not None:
            return await _consent_response(lkapi, session)

        consent_event = ConsentEvent(
            session_id=session_id,
            accepted=body.accepted,
            consent_text_version=body.consent_text_version,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        db.add(consent_event)
        metrics.CONSENT_DECISIONS.labels(accepted=str(body.accepted)).inc()

        if not body.accepted:
            if settings.allow_unrecorded_fallback:
                session.recording_enabled = False
                session.state = SessionState.ACTIVE
            else:
                session.state = SessionState.CONSENT_DECLINED
            await db.commit()
            return schemas.ConsentResponse(
                accepted=False,
                session_state=session.state.value,
                livekit=None,
                recording_enabled=session.recording_enabled,
            )

        session.state = SessionState.ACTIVE
        await _start_room(lkapi, session)
        await db.commit()
        return await _consent_response(lkapi, session)


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


# NOTE: LiveKit's CreateRoomRequest does not expose a max_duration field in the
# installed livekit-api 1.2.0. The spec's room_max_duration_seconds is retained
# in settings but is not currently enforced at room creation. Enforcement may be
# added via server-side agent/egress limits or a sweeper job.
async def _consent_response(lkapi: api.LiveKitAPI, session: Session) -> schemas.ConsentResponse:
    if session.state == SessionState.CONSENT_DECLINED:
        return schemas.ConsentResponse(
            accepted=False,
            session_state=session.state.value,
            livekit=None,
            recording_enabled=session.recording_enabled,
        )

    if not session.caller_identity:
        _error(
            "session_not_ready",
            "Caller identity not assigned",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    assert session.caller_identity is not None

    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.livekit_token_ttl_seconds
    )
    token = build_token(
        room_name=session.room_name,
        identity=session.caller_identity,
        role=ParticipantRole.CALLER,
        display_name=None,
        session_id=session.id,
        ttl_seconds=settings.livekit_token_ttl_seconds,
    )
    return schemas.ConsentResponse(
        accepted=True,
        session_state=session.state.value,
        livekit=schemas.LiveKitCredentials(
            ws_url=settings.livekit_url,
            token=token,
            room_name=session.room_name,
            identity=session.caller_identity,
            expires_at=expires_at,
        ),
        recording_enabled=session.recording_enabled,
    )


@router.post("/join", response_model=schemas.JoinResponse)
async def join_session(
    body: schemas.JoinRequest,
    request: Request,
    support: SupportTokenPayload = Depends(require_support_user),
):
    """Support joins by code. Auth: support user."""
    ip = client_ip(request)
    if not await check_rate_limit(
        key=f"join:ip:{ip}", limit=100, window_seconds=3600
    ):
        _error(
            "rate_limit",
            "Too many join attempts from this IP",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
    if not await check_rate_limit(
        key=f"join:user:{support.user_id}", limit=10, window_seconds=60
    ):
        _error(
            "rate_limit",
            "Too many join attempts",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    code = normalize_code(body.join_code)
    if len(code) != settings.join_code_length:
        _error("code_not_found", "Invalid join code length", status.HTTP_404_NOT_FOUND)

    lkapi: api.LiveKitAPI = request.app.state.lkapi

    async with AsyncSessionLocal() as db:
        session_result = await db.execute(
            select(Session)
            .where(Session.join_code == code)
            .where(Session.state.in_([SessionState.PENDING, SessionState.ACTIVE]))
        )
        session = session_result.scalar_one_or_none()
        if session is None:
            _error("code_not_found", "Join code not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        if session.join_code_expires_at and session.join_code_expires_at < datetime.now(UTC):
            _error("code_expired", "Join code has expired", status.HTTP_410_GONE)

        if session.state not in (SessionState.PENDING, SessionState.ACTIVE):
            _error("session_not_joinable", "Session is not joinable", status.HTTP_409_CONFLICT)

        support_identity = make_support_identity(support.user_id)
        if session.support_identity and session.support_identity != support_identity:
            _error("role_occupied", "Support role is already occupied", status.HTTP_409_CONFLICT)

        session.support_identity = support_identity
        session.support_user_id = support.user_id

        # If the caller has not yet consented, the session is still pending.
        # We allow support to join early so they are ready; the room only exists
        # after consent. State transitions happen via webhooks once the caller
        # connects.
        if session.state == SessionState.PENDING:
            session.state = SessionState.ACTIVE
            await _start_room(lkapi, session)
        else:
            # Ensure room metadata reflects the updated session.
            await dispatch.update_room_metadata(lkapi, session)

        # Upsert participant row. The unique index enforces one support per session.
        existing_support = await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.role == ParticipantRole.SUPPORT,
            )
        )
        if existing_support.scalar_one_or_none() is not None:
            _error("role_occupied", "Support role is already occupied", status.HTTP_409_CONFLICT)

        participant = SessionParticipant(
            session_id=session.id,
            role=ParticipantRole.SUPPORT,
            identity=support_identity,
            display_name=body.display_name,
            joined_at=datetime.now(UTC),
        )
        db.add(participant)

        await db.commit()

        caller_display_name = None
        caller_result = await db.execute(
            select(SessionParticipant).where(
                SessionParticipant.session_id == session.id,
                SessionParticipant.role == ParticipantRole.CALLER,
            )
        )
        caller_participant = caller_result.scalar_one_or_none()
        if caller_participant:
            caller_display_name = caller_participant.display_name

        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.livekit_token_ttl_seconds
        )
        token = build_token(
            room_name=session.room_name,
            identity=support_identity,
            role=ParticipantRole.SUPPORT,
            display_name=body.display_name,
            session_id=session.id,
            ttl_seconds=settings.livekit_token_ttl_seconds,
        )

        response = schemas.JoinResponse(
            session_id=session.id,
            livekit=schemas.LiveKitCredentials(
                ws_url=settings.livekit_url,
                token=token,
                room_name=session.room_name,
                identity=support_identity,
                expires_at=expires_at,
            ),
            ai_enabled=session.ai_enabled,
            agent_mode=session.agent_mode,
            recording_enabled=session.recording_enabled,
            caller_display_name=caller_display_name,
        )
        metrics.JOINS_ATTEMPTED.labels(result="success").inc()
        return response


@router.post("/{session_id}/token/refresh", response_model=schemas.LiveKitCredentials)
async def refresh_token(
    session_id: uuid.UUID,
    request: Request,
    caller: CallerTokenPayload = Depends(require_caller_session),
):
    """Fresh LiveKit token for the same identity."""
    if caller.session_id != session_id:
        _error("forbidden", "Token does not authorize this session", status.HTTP_403_FORBIDDEN)

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None or session.caller_identity != caller.identity:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None
        assert session.caller_identity is not None

        expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.livekit_token_ttl_seconds
        )
        token = build_token(
            room_name=session.room_name,
            identity=session.caller_identity,
            role=ParticipantRole.CALLER,
            display_name=None,
            session_id=session.id,
            ttl_seconds=settings.livekit_token_ttl_seconds,
        )
        return schemas.LiveKitCredentials(
            ws_url=settings.livekit_url,
            token=token,
            room_name=session.room_name,
            identity=session.caller_identity,
            expires_at=expires_at,
        )


@router.get("/{session_id}", response_model=schemas.SessionDetail)
async def get_session(
    session_id: uuid.UUID,
    request: Request,
):
    """Full session state. Also the agent worker's reconciliation source."""
    from ..deps import require_session_access
    await require_session_access(request, session_id)

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)

        participants_result = await db.execute(
            select(SessionParticipant).where(SessionParticipant.session_id == session_id)
        )
        participants = participants_result.scalars().all()

        recordings_result = await db.execute(
            select(Recording).where(Recording.session_id == session_id)
        )
        recordings = recordings_result.scalars().all()

        assert session is not None
        return schemas.SessionDetail(
            session_id=session.id,
            state=session.state.value,
            room_name=session.room_name,
            ai_enabled=session.ai_enabled,
            agent_mode=session.agent_mode,
            recording_enabled=session.recording_enabled,
            metadata_version=session.metadata_version,
            participants=[
                schemas.ParticipantInfo(
                    role=p.role,
                    identity=p.identity,
                    display_name=p.display_name,
                    joined_at=p.joined_at,
                    left_at=p.left_at,
                )
                for p in participants
            ],
            recordings=[
                schemas.RecordingInfo(
                    kind=r.kind,
                    role=r.role,
                    state=r.state,
                    mime_type=r.mime_type,
                    duration_ms=r.duration_ms,
                    size_bytes=r.size_bytes,
                    gcs_uri=r.gcs_uri,
                )
                for r in recordings
            ],
            started_at=session.started_at,
            ended_at=session.ended_at,
        )


@router.post("/{session_id}/end", status_code=202)
async def end_session(
    session_id: uuid.UUID,
    request: Request,
    caller: CallerTokenPayload = Depends(require_caller_session),
):
    """Delete the LiveKit room, which cascades to egress and the agent job."""
    if caller.session_id != session_id:
        _error("forbidden", "Token does not authorize this session", status.HTTP_403_FORBIDDEN)

    lkapi: api.LiveKitAPI = request.app.state.lkapi

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        await lkapi.room.delete_room(api.DeleteRoomRequest(room=session.room_name))
        session.state = SessionState.COMPLETED
        session.ended_at = datetime.now(UTC)
        await db.commit()

    return {"status": "ending"}


@router.delete("/{session_id}/data")
async def purge_session_data(
    session_id: uuid.UUID,
    request: Request,
    support: SupportTokenPayload = Depends(require_admin),
):
    """Purge media, transcript rows and exports. Requires an admin claim."""
    gcs_client = request.app.state.gcs_client

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        objects_deleted = await storage.delete_session_objects(gcs_client, session_id)

        recordings_result = await db.execute(
            select(Recording).where(Recording.session_id == session_id)
        )
        for rec in recordings_result.scalars().all():
            await db.delete(rec)

        # Count utterances before deleting them for the tombstone.
        from ..models import TranscriptUtterance
        count_result = await db.execute(
            select(func.count()).where(TranscriptUtterance.session_id == session_id)
        )
        utterances_deleted = count_result.scalar() or 0
        await db.execute(
            delete(TranscriptUtterance).where(TranscriptUtterance.session_id == session_id)
        )

        # Clear sensitive fields but keep the skeleton row.
        session.state = SessionState.PURGED
        session.join_code = None
        session.caller_identity = None
        session.support_identity = None
        session.support_user_id = None
        session.ai_enabled = False
        session.recording_enabled = False

        tombstone = DataPurge(
            session_id=session_id,
            requested_by=support.email,
            objects_deleted=objects_deleted,
            utterances_deleted=utterances_deleted,
        )
        db.add(tombstone)
        await db.commit()

    return {
        "session_id": str(session_id),
        "objects_deleted": objects_deleted,
        "utterances_deleted": utterances_deleted,
    }
