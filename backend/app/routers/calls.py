"""Open multi-party calls: join a plain A/V room with just a link.

Unlike support sessions there is no caller/support role split, no consent, and
no agent dispatch -- anyone holding ``/call/<call_id>`` can join,
Google Meet style. Gated by ``settings.open_calls_enabled``. Rooms are prefixed
``call_`` so guest tokens can never address a support session room (``rs_``).

With ``open_call_recording_enabled``, any participant can start/stop
recording; each published track is then saved to its own file under
``calls/<call_id>/`` in ``GCS_BUCKET`` (see services/call_recording.py).
Recording endpoints authenticate with the caller's own LiveKit token, so only
someone already admitted to that call can toggle it.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from livekit import api
from livekit.api.access_token import TokenVerifier

from .. import schemas
from ..base_path import url_for_path
from ..config import get_settings
from ..services import call_recording, storage
from ..services.livekit_tokens import (
    build_guest_token,
    make_guest_identity,
    open_call_room_name,
)
from ..services.rate_limit import check_rate_limit, client_ip

router = APIRouter(tags=["calls"])
settings = get_settings()

# Lowercase letters/digits/dashes; long enough that unguessable ids are the
# norm but short readable ids like "team-standup" still work.
_CALL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST):
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": {}}},
    )


def _require_enabled() -> None:
    if not settings.open_calls_enabled:
        _error("not_found", "Open calls are disabled", status.HTTP_404_NOT_FOUND)


def _normalize_call_id(call_id: str) -> str:
    normalized = call_id.strip().lower()
    if not _CALL_ID_RE.match(normalized):
        _error(
            "invalid_call_id",
            "Call id must be 3-64 chars of letters, digits, and dashes",
        )
    return normalized


def new_call_id() -> str:
    """Meet-style ``xxx-xxxx-xxx`` id with ~50 bits of entropy."""
    alphabet = "abcdefghijkmnopqrstuvwxyz"
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(n)) for n in (3, 4, 3)
    ]
    return "-".join(parts)


def _require_recording_allowed() -> None:
    if not settings.open_call_recording_enabled:
        _error(
            "recording_disabled",
            "Recording is not enabled on this server",
            status.HTTP_403_FORBIDDEN,
        )


def _call_participant(authorization: str | None, room_name: str) -> str:
    """Verify the bearer LiveKit token is for this call; return a display label."""
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        _error("unauthorized", "Missing call token", status.HTTP_401_UNAUTHORIZED)
    try:
        claims = TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret).verify(
            token
        )
    except Exception:  # noqa: BLE001
        _error("unauthorized", "Invalid call token", status.HTTP_401_UNAUTHORIZED)
    if claims.video is None or claims.video.room != room_name:
        _error("forbidden", "Token is not for this call", status.HTTP_403_FORBIDDEN)
    return claims.name or claims.identity


@router.get("/call")
async def new_call(request: Request) -> Response:
    """Start a fresh call: redirect to a newly generated call link."""
    _require_enabled()
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": url_for_path(request, f"/call/{new_call_id()}")},
    )


@router.get("/call/{call_id}")
async def open_call_link(call_id: str, request: Request) -> Response:
    """Shareable call link: redirects into the web client in open-call mode."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": url_for_path(request, f"/support-web/?call={normalized}")},
    )


@router.get("/v1/calls/{call_id}", response_model=schemas.OpenCallInfo)
async def open_call_info(call_id: str, request: Request):
    """Pre-join info so the client can show a recording notice before joining."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    active = False
    if settings.open_call_recording_enabled:
        state = await call_recording.get_state(
            request.app.state.lkapi, open_call_room_name(normalized)
        )
        active = bool(state.get("recording"))
    return schemas.OpenCallInfo(
        call_id=normalized,
        recording_enabled=settings.open_call_recording_enabled,
        recording_active=active,
    )


@router.post("/v1/calls/{call_id}/join", response_model=schemas.OpenCallJoinResponse)
async def join_open_call(
    call_id: str, body: schemas.OpenCallJoinRequest, request: Request
):
    """Mint a guest LiveKit token for an open call. Auth: none (link-only)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)

    ip = client_ip(request)
    if not await check_rate_limit(key=f"call-join:ip:{ip}", limit=30, window_seconds=60):
        _error(
            "rate_limit",
            "Too many join attempts",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    room_name = open_call_room_name(normalized)
    lkapi: api.LiveKitAPI = request.app.state.lkapi
    # CreateRoom returns the existing room if it is already live, so every
    # joiner can call it; it just pins the participant cap and empty timeout.
    await lkapi.room.create_room(
        api.CreateRoomRequest(
            name=room_name,
            empty_timeout=settings.room_empty_timeout_seconds,
            departure_timeout=settings.room_departure_timeout_seconds,
            max_participants=settings.open_call_max_participants,
        )
    )

    display_name = (body.display_name or "").strip() or None
    identity = make_guest_identity(display_name)
    token = build_guest_token(
        room_name=room_name,
        identity=identity,
        display_name=display_name,
        ttl_seconds=settings.livekit_token_ttl_seconds,
    )
    return schemas.OpenCallJoinResponse(
        call_id=normalized,
        livekit=schemas.LiveKitCredentials(
            ws_url=settings.livekit_url,
            token=token,
            room_name=room_name,
            identity=identity,
            expires_at=datetime.now(UTC)
            + timedelta(seconds=settings.livekit_token_ttl_seconds),
        ),
        recording_enabled=settings.open_call_recording_enabled,
    )


@router.get("/v1/calls/{call_id}/recordings", response_model=schemas.CallRecordingList)
async def list_call_recordings(call_id: str, request: Request):
    """Recordings for a call, with short-lived signed URLs to play them.

    Auth: none -- anyone holding the call link may view its recordings, by
    product decision. Call ids from GET /call are unguessable; hand-picked ids
    like "team-standup" are not.
    """
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    ip = client_ip(request)
    if not await check_rate_limit(key=f"call-recs:ip:{ip}", limit=60, window_seconds=60):
        _error("rate_limit", "Too many requests", status.HTTP_429_TOO_MANY_REQUESTS)
    items = await asyncio.to_thread(
        storage.list_call_recordings, request.app.state.gcs_client, normalized
    )
    return schemas.CallRecordingList(
        call_id=normalized,
        recordings=[schemas.CallRecordingFile(**i) for i in items],
    )


@router.post("/v1/calls/{call_id}/end", status_code=status.HTTP_202_ACCEPTED)
async def end_open_call(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """Hang up for everyone. Auth: this call's LiveKit token (any participant)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    who = _call_participant(authorization, room_name)
    await call_recording.end_call(request.app.state.lkapi, room_name, who)
    return {"ended": True, "ended_by": who}


@router.post("/v1/calls/{call_id}/recording/start", response_model=schemas.CallRecordingState)
async def start_call_recording(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """Start recording every participant's tracks. Auth: this call's LiveKit token."""
    _require_enabled()
    _require_recording_allowed()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    who = _call_participant(authorization, room_name)
    state = await call_recording.start(request.app.state.lkapi, normalized, room_name, who)
    return schemas.CallRecordingState(recording=True, started_by=state.get("started_by"))


@router.post("/v1/calls/{call_id}/recording/stop", response_model=schemas.CallRecordingState)
async def stop_call_recording(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """Stop all recordings in the call. Auth: this call's LiveKit token."""
    _require_enabled()
    _require_recording_allowed()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    who = _call_participant(authorization, room_name)
    await call_recording.stop(request.app.state.lkapi, room_name, who)
    return schemas.CallRecordingState(recording=False)


@router.post("/v1/calls/{call_id}/recording/sync", response_model=schemas.CallRecordingState)
async def sync_call_recording(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """Nudge after publishing a track so it is recorded without waiting for the
    background loop. No-op when recording is off."""
    _require_enabled()
    _require_recording_allowed()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    _call_participant(authorization, room_name)
    lkapi = request.app.state.lkapi
    await call_recording.reconcile(lkapi, normalized, room_name)
    state = await call_recording.get_state(lkapi, room_name)
    if state.get("recording"):
        call_recording.ensure_loop(lkapi, normalized, room_name)
    return schemas.CallRecordingState(
        recording=bool(state.get("recording")), started_by=state.get("started_by")
    )
