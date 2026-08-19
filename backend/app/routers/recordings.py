"""Recording metadata and signed download URLs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, NoReturn

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from google.cloud import storage as gcs
from sqlalchemy import select

from .. import schemas
from ..config import get_settings
from ..core.firebase_auth import AuthError, SupportPrincipal
from ..db import AsyncSessionLocal
from ..deps import CallerTokenPayload, get_support_verifier
from ..models import Recording, RecordingState, Session
from ..services import storage

router = APIRouter(prefix="/v1/sessions", tags=["recordings"])
settings = get_settings()


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


async def _require_recording_viewer(
    request: Request,
    session_id: uuid.UUID,
) -> SupportPrincipal | CallerTokenPayload:
    """Authorize support users, and callers if ``allow_caller_download`` is enabled."""
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        _error("missing_token", "Authorization header required", status.HTTP_401_UNAUTHORIZED)

    token = auth.split(" ", 1)[1].strip()

    # Try support user token first.
    try:
        return get_support_verifier().verify(token)
    except AuthError:
        pass

    # Fall back to a caller session token when caller downloads are enabled.
    if settings.allow_caller_download:
        try:
            payload = jwt.decode(
                token,
                settings.caller_jwt_secret,
                algorithms=["HS256"],
                options={"require": ["exp"]},
            )
        except jwt.InvalidTokenError:
            pass
        else:
            if payload.get("type") == "caller_session":
                try:
                    token_session_id = uuid.UUID(payload["session_id"])
                except (KeyError, ValueError, TypeError):
                    pass
                else:
                    if token_session_id == session_id:
                        return CallerTokenPayload(
                            session_id=session_id,
                            identity=payload.get("sub", ""),
                        )

    _error("unauthorized", "Valid authorization required", status.HTTP_401_UNAUTHORIZED)


@router.get("/{session_id}/recordings", response_model=schemas.RecordingsResponse)
async def list_recordings(
    session_id: uuid.UUID,
    request: Request,
    _viewer: Annotated[
        SupportPrincipal | CallerTokenPayload,
        Depends(_require_recording_viewer),
    ],
) -> schemas.RecordingsResponse:
    """List the session's media with V4 signed URLs.

    The backend never proxies media bytes -- egress writes straight to GCS and
    clients fetch via short-lived signed URLs (SIGNED_URL_TTL_SECONDS).
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
    gcs_client: gcs.Client,
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
