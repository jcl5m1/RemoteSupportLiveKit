"""AI agent control plane. See docs/02-architecture.md § control plane."""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api

from .. import metrics, schemas
from ..config import get_settings
from ..db import AsyncSessionLocal
from ..deps import (
    AuthError,
    SupportTokenPayload,
    _get_verifier,
    require_support_user,
)
from ..models import AgentEvent, Session
from ..services.dispatch import update_room_metadata
from ..services.rate_limit import check_rate_limit

router = APIRouter(prefix="/v1/sessions", tags=["agent"])
settings = get_settings()


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


async def _require_service_or_support(request: Request) -> SupportTokenPayload | None:
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


@router.post("/{session_id}/agent", response_model=schemas.AgentToggleResponse)
async def set_ai_enabled(
    session_id: uuid.UUID,
    body: schemas.AgentToggleRequest,
    request: Request,
    support: SupportTokenPayload = Depends(require_support_user),
):
    """Enable/disable the AI agent's speech. **Support auth only** (FR-4.1)."""
    if not await check_rate_limit(
        key=f"agent_toggle:session:{session_id}", limit=30, window_seconds=60
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "code": "rate_limit",
                    "message": "Too many AI toggle requests",
                    "details": {},
                }
            },
        )

    lkapi: api.LiveKitAPI = request.app.state.lkapi
    applied_at = datetime.now(UTC)

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        old_value = session.ai_enabled
        new_value = body.enabled
        session.ai_enabled = new_value

        event = AgentEvent(
            session_id=session_id,
            event_type="ai_enabled_changed",
            actor=support.email,
            payload={
                "reason": body.reason,
                "old": old_value,
                "new": new_value,
            },
        )
        db.add(event)

        session.metadata_version += 1
        await db.commit()

        try:
            await update_room_metadata(lkapi, session)
        except Exception:  # noqa: BLE001
            # Per spec: do NOT roll back DB. Agent reconciles on heartbeat.
            pass

        metrics.AI_TOGGLES.labels(enabled=str(new_value)).inc()
        return schemas.AgentToggleResponse(
            ai_enabled=session.ai_enabled,
            metadata_version=session.metadata_version,
            applied_at=applied_at,
        )


@router.post("/{session_id}/agent/mode")
async def set_agent_mode(
    session_id: uuid.UUID,
    body: schemas.AgentModeRequest,
    request: Request,
    principal: SupportTokenPayload | None = Depends(_require_service_or_support),
):
    """Record a mode transition. Called by the agent worker (service auth) or
    forced by support.
    """
    lkapi: api.LiveKitAPI = request.app.state.lkapi
    actor = principal.email if principal else "agent"

    async with AsyncSessionLocal() as db:
        session = await db.get(Session, session_id)
        if session is None:
            _error("not_found", "Session not found", status.HTTP_404_NOT_FOUND)
        assert session is not None

        old_mode = session.agent_mode
        new_mode = body.mode
        if old_mode == new_mode:
            return {
                "agent_mode": session.agent_mode.value,
                "metadata_version": session.metadata_version,
            }

        session.agent_mode = new_mode

        event = AgentEvent(
            session_id=session_id,
            event_type="mode_changed",
            actor=actor,
            payload={"old": old_mode.value, "new": new_mode.value},
        )
        db.add(event)

        session.metadata_version += 1
        await db.commit()

        try:
            await update_room_metadata(lkapi, session)
        except Exception:  # noqa: BLE001
            pass

        return {
            "agent_mode": session.agent_mode.value,
            "metadata_version": session.metadata_version,
        }
