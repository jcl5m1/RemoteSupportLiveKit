"""Transcript ingest, read, and export."""

from __future__ import annotations

import base64
import hmac
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select

from .. import metrics, schemas
from ..config import get_settings
from ..db import AsyncSessionLocal
from ..deps import (
    AuthError,
    SupportTokenPayload,
    _get_verifier,
    require_service_key,
    require_session_access,
)
from ..models import Session, TranscriptUtterance
from ..services import transcripts as transcripts_service
from ..services.rate_limit import check_rate_limit

router = APIRouter(prefix="/v1/sessions", tags=["transcripts"])
settings = get_settings()

_MAX_PAGE_LIMIT = 1000


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


async def _require_support_or_service(request: Request) -> SupportTokenPayload | None:
    """Accept either a valid support user token or a valid service key."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        try:
            return _get_verifier().verify(token)
        except AuthError:
            pass

    if hmac.compare_digest(
        request.headers.get("X-Service-Key", ""), settings.service_api_key
    ):
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Support user or service key required"},
    )


@router.post("/{session_id}/utterances", response_model=schemas.UtteranceIngestResponse)
async def ingest_utterances(
    session_id: uuid.UUID,
    body: schemas.UtteranceIngestRequest,
    _: None = Depends(require_service_key),
):
    """Batched, idempotent utterance ingest. Auth: service (agent worker).

    INSERT ... ON CONFLICT (session_id, client_utterance_id) DO NOTHING.
    That index is what makes the agent's retry-with-backoff loop safe.

    Only finalized utterances arrive here; interims are never persisted (FR-6.3).
    """
    settings = get_settings()
    if not await check_rate_limit(
        key=f"utterances:session:{session_id}",
        limit=settings.utterance_rate_limit,
        window_seconds=settings.utterance_rate_limit_window_seconds,
    ):
        metrics.TRANSCRIPT_BATCHES.labels(status="rate_limited").inc()
        _error("rate_limit", "Too many utterance batches", status.HTTP_429_TOO_MANY_REQUESTS)

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            metrics.TRANSCRIPT_BATCHES.labels(status="not_found").inc()
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        try:
            result = await transcripts_service.ingest_utterances(
                db, session_id, body.utterances
            )
            await db.commit()
        except Exception:
            metrics.TRANSCRIPT_BATCHES.labels(status="failed").inc()
            raise
        for u in body.utterances:
            metrics.UTTERANCES_INGESTED.labels(
                role=u.role.value, source=u.source.value
            ).inc()
        metrics.TRANSCRIPT_BATCHES.labels(status="success").inc()
        return schemas.UtteranceIngestResponse(**result)


@router.get("/{session_id}/transcript", response_model=schemas.TranscriptPage)
async def get_transcript(
    session_id: uuid.UUID,
    request: Request,
    since_ms: int = 0,
    limit: int = 500,
    cursor: str | None = None,
):
    """Ordered utterances with a cursor. The support panel reconciles against
    this every 10s; the low-latency path is the lk.transcription text stream.
    """
    await require_session_access(request, session_id)

    page_limit = min(limit, _MAX_PAGE_LIMIT)
    cursor_start_ms = since_ms
    cursor_id = 0
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            parts = decoded.split(",")
            cursor_start_ms = int(parts[0])
            cursor_id = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            _error("invalid_cursor", "Invalid cursor")

    async with AsyncSessionLocal() as db:
        seq_col = func.row_number().over(
            order_by=[TranscriptUtterance.start_ms, TranscriptUtterance.id]
        ).label("seq")

        stmt = (
            select(TranscriptUtterance, seq_col)
            .where(TranscriptUtterance.session_id == session_id)
            .where(
                (TranscriptUtterance.start_ms > cursor_start_ms)
                | (
                    (TranscriptUtterance.start_ms == cursor_start_ms)
                    & (TranscriptUtterance.id > cursor_id)
                )
            )
            .order_by(TranscriptUtterance.start_ms, TranscriptUtterance.id)
            .limit(page_limit + 1)
        )

        result = await db.execute(stmt)
        rows = result.all()

        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]

        utterances = []
        for row in page_rows:
            u = row[0]
            seq = row[1]
            utterances.append(
                schemas.UtteranceOut(
                    seq=seq,
                    client_utterance_id=u.client_utterance_id,
                    role=u.role,
                    identity=u.identity,
                    source=u.source,
                    start_ms=u.start_ms,
                    end_ms=u.end_ms,
                    text=u.text,
                    language=u.language,
                    confidence=u.confidence,
                    agent_mode=u.agent_mode,
                    ai_enabled=u.ai_enabled,
                )
            )

        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1][0]
            next_cursor = base64.urlsafe_b64encode(
                f"{last.start_ms},{last.id}".encode()
            ).decode()

        return schemas.TranscriptPage(utterances=utterances, next_cursor=next_cursor)


@router.post("/{session_id}/transcript/export", status_code=202)
async def export_transcript(
    session_id: uuid.UUID,
    request: Request,
    support: SupportTokenPayload | None = Depends(_require_support_or_service),
):
    """Force the JSONL/VTT/TXT/session.json export. Normally triggered by the
    room_finished webhook; this endpoint exists for retry. Idempotent.
    """
    del support  # auth only
    gcs_client = request.app.state.gcs_client

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        urls = await transcripts_service.export_transcript(db, gcs_client, session)
        return {
            "status": "exporting",
            "jsonl_url": urls["jsonl"],
            "vtt_url": urls["vtt"],
            "txt_url": urls["txt"],
            "session_json_url": urls["session"],
        }
