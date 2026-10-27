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
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from livekit import api
from livekit.api.access_token import TokenVerifier

from .. import schemas
from ..base_path import url_for_path
from ..config import get_settings
from ..services import call_events, call_recording, media_check, storage
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


def _require_recording_allowed() -> None:
    if not settings.open_call_recording_enabled:
        _error(
            "recording_disabled",
            "Recording is not enabled on this server",
            status.HTTP_403_FORBIDDEN,
        )


def _call_participant(authorization: str | None, room_name: str) -> str:
    """Verify the bearer LiveKit token is for this call; return a display label."""
    identity, name = _call_claims(authorization, room_name)
    return name or identity


def _call_claims(authorization: str | None, room_name: str) -> tuple[str, str | None]:
    """Verify the bearer LiveKit token is for this call; return (identity, name)."""
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
    return claims.identity, claims.name or None


_WEB_CLIENT_INDEX = Path(__file__).resolve().parent.parent / "static" / "support-web" / "index.html"


def call_page(request: Request, call_id: str | None = None) -> HTMLResponse:
    """The web client's join screen, served at the meeting URL itself.

    The page lives in static/support-web/ (assets stay under /support-web/),
    but meeting links are clean: /call/<code>, or /call and / for a new
    meeting. The base path and code are injected so the client needs no
    query string; JSON-encoding keeps the values inert inside <script>.
    """
    base = request.scope.get("root_path", "")
    html = _WEB_CLIENT_INDEX.read_text()
    client_config = {
        "googleMapsKey": settings.google_maps_api_key or None,
        "googleMapsMapId": settings.google_maps_map_id or None,
    }
    boot = (
        f"<script>window.APP_BASE = {json.dumps(base)};"
        f" window.APP_CALL_ID = {json.dumps(call_id)};"
        f" window.APP_CONFIG = {json.dumps(client_config)};</script>\n"
    )
    script = f'  <script src="{base}/support-web/app.js'
    html = html.replace('<script src="app.js', boot + script, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _code_from_query(request: Request) -> tuple[str | None, bool, list[tuple[str, str]]]:
    """Meeting code from the query: ``(code, named, remaining params)``.

    The canonical form is the named ``?call=<code>``; the parameter name
    identifies the kind of support link (``call`` for meetings, ``session``
    for support sessions, ...). Nameless shorthands ``?=<code>`` and a bare
    ``?<code>`` are also accepted (``named=False``) and get redirected.
    """
    pairs = parse_qsl(request.url.query, keep_blank_values=True)
    for key, named in (("call", True), ("", False)):
        for i, (k, v) in enumerate(pairs):
            if k == key and v:
                return v, named, pairs[:i] + pairs[i + 1 :]
    if len(pairs) == 1 and pairs[0][1] == "" and "=" not in request.url.query:
        return pairs[0][0], False, []
    return None, False, pairs


def _meeting_redirect(
    request: Request, code: str, rest: list[tuple[str, str]] | None = None
) -> Response:
    query = urlencode([("call", code), *(rest or [])])
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": url_for_path(request, f"/?{query}")},
    )


def meeting_entry(request: Request) -> Response:
    """``/`` — the web client entry point. ``?call=<code>`` is a meeting (served
    directly); other shorthands redirect to it; no code shows the join screen
    for a new meeting. Other parameters (e.g. ``?session=``) pass through to
    the client, which dispatches on the parameter name."""
    code, named, rest = _code_from_query(request)
    if code is None:
        return call_page(request)
    normalized = re.sub(r"[\s_]+", "-", code.strip().lower())
    if not _CALL_ID_RE.match(normalized):
        # Let the join screen show the code with its validation message.
        return call_page(request, normalized)
    if not named or normalized != code:
        return _meeting_redirect(request, normalized, rest)
    return call_page(request, normalized)


@router.get("/call")
async def new_call(request: Request) -> Response:
    """Legacy/short entry: same as ``/`` (``/call?call=x`` etc. included)."""
    _require_enabled()
    code, _named, rest = _code_from_query(request)
    if code is None:
        query = f"?{urlencode(rest)}" if rest else ""
        return Response(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": url_for_path(request, f"/{query}")},
        )
    normalized = re.sub(r"[\s_]+", "-", code.strip().lower())
    return _meeting_redirect(request, normalized, rest)


@router.get("/call/{call_id}")
async def open_call_link(call_id: str, request: Request) -> Response:
    """Path-style link ``/call/<code>``: redirects to the canonical ``/?call=<code>``."""
    _require_enabled()
    return _meeting_redirect(request, _normalize_call_id(call_id))


@router.get("/v1/calls/{call_id}", response_model=schemas.OpenCallInfo)
async def open_call_info(call_id: str, request: Request):
    """Pre-join info: recording notice, and whether the meeting is running."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    room = await call_recording.get_room(
        request.app.state.lkapi, open_call_room_name(normalized)
    )
    recording = bool(call_recording.room_state(room).get("recording"))
    return schemas.OpenCallInfo(
        call_id=normalized,
        recording_enabled=settings.open_call_recording_enabled,
        recording_active=settings.open_call_recording_enabled and recording,
        room_active=room is not None,
        participant_count=room.num_participants if room is not None else 0,
        maps_api_key=settings.google_maps_api_key or None,
        maps_map_id=settings.google_maps_map_id or None,
    )


@router.post("/v1/calls/{call_id}/join", response_model=schemas.OpenCallJoinResponse)
async def join_open_call(
    call_id: str, body: schemas.OpenCallJoinRequest, request: Request
):
    """Mint a guest LiveKit token for an open call. Auth: none (link-only)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)

    ip = client_ip(request)
    if not await check_rate_limit(
        key=f"call-join:ip:{ip}", limit=settings.open_call_join_rate_limit, window_seconds=60
    ):
        _error(
            "rate_limit",
            "Too many join attempts",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    room_name = open_call_room_name(normalized)
    lkapi: api.LiveKitAPI = request.app.state.lkapi
    # CreateRoom returns the existing room if it is already live, so every
    # joiner can call it; it just pins the participant cap and empty timeout.
    room = await lkapi.room.create_room(
        api.CreateRoomRequest(
            name=room_name,
            empty_timeout=settings.room_empty_timeout_seconds,
            departure_timeout=settings.room_departure_timeout_seconds,
            max_participants=settings.open_call_max_participants,
        )
    )

    display_name = (body.display_name or "").strip() or None
    identity = make_guest_identity(display_name)
    sid = getattr(room, "sid", None) or None
    call_events.remember_sid(room_name, sid)
    await call_events.log_server_event(
        normalized, sid, "join", identity, display_name,
        {"recording_enabled": settings.open_call_recording_enabled},
    )
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
    logs = await asyncio.to_thread(
        storage.list_call_event_logs, request.app.state.gcs_client, normalized
    )
    photos = await asyncio.to_thread(
        storage.list_call_photos, request.app.state.gcs_client, normalized
    )
    return schemas.CallRecordingList(
        call_id=normalized,
        recordings=[schemas.CallRecordingFile(**i) for i in items],
        event_logs=[schemas.CallEventLogFile(**i) for i in logs],
        photos=[schemas.CallPhotoFile(**i) for i in photos],
    )


@router.post("/v1/calls/{call_id}/end", status_code=status.HTTP_202_ACCEPTED)
async def end_open_call(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """End the meeting: stop recordings and close the room. The web client calls
    this when the last person hangs up (abrupt exits fall back to the room's
    departure timeout). Auth: this call's LiveKit token."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    identity, name = _call_claims(authorization, room_name)
    who = name or identity
    lkapi = request.app.state.lkapi
    sid = await call_events.room_sid(lkapi, room_name)
    await call_events.log_server_event(normalized, sid, "call_end", identity, name)
    await call_recording.end_call(lkapi, room_name, who)
    await call_events.export_now(request.app.state.gcs_client, normalized, sid)
    return {"ended": True, "ended_by": who}


@router.get("/v1/calls/{call_id}/media-check")
async def check_shared_media(
    call_id: str,
    url: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Validate a ``/show <url>`` chat link before it is sent: it must resolve
    (redirects followed, public hosts only) and is classified so receivers can
    present it full screen. ``422 media_unavailable`` carries a user-facing
    reason. Auth: this call's LiveKit token."""
    _require_enabled()
    room_name = open_call_room_name(_normalize_call_id(call_id))
    _call_participant(authorization, room_name)
    ip = client_ip(request)
    if not await check_rate_limit(key=f"media-check:ip:{ip}", limit=30, window_seconds=60):
        _error("rate_limit", "Too many link checks", status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        info = await media_check.check_url(url)
    except media_check.MediaCheckError as exc:
        _error("media_unavailable", str(exc), status.HTTP_422_UNPROCESSABLE_CONTENT)
    return info.as_dict()


MAX_PHOTO_BYTES = 25 * 1024 * 1024
_PHOTO_MAGIC = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}


@router.post(
    "/v1/calls/{call_id}/photos",
    response_model=schemas.CallPhotoFile,
    status_code=status.HTTP_201_CREATED,
)
async def upload_call_photo(
    call_id: str,
    request: Request,
    requested_by: str | None = None,
    width: int | None = None,
    height: int | None = None,
    authorization: str | None = Header(default=None),
):
    """A full-resolution photo from a participant's (phone) camera, taken when
    someone in the call asked for one. Raw image body (JPEG/PNG/WebP, ≤25 MB);
    stored beside the recordings and logged as a ``photo`` event.
    Auth: this call's LiveKit token (the photo is attributed to its identity)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    identity, name = _call_claims(authorization, room_name)
    if not await check_rate_limit(key=f"call-photo:{identity}", limit=20, window_seconds=60):
        _error("rate_limit", "Too many photos", status.HTTP_429_TOO_MANY_REQUESTS)
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in _PHOTO_MAGIC:
        _error("invalid_photo", "Photo must be JPEG, PNG or WebP",
               status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    too_big = ("photo_too_large", "Photo is larger than 25 MB",
               status.HTTP_413_CONTENT_TOO_LARGE)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_PHOTO_BYTES:
        _error(*too_big)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_PHOTO_BYTES:
            _error(*too_big)
    if not any(bytes(data[:8]).startswith(m) for m in _PHOTO_MAGIC[ctype]):
        _error("invalid_photo", "Photo data does not match its type")
    info = await asyncio.to_thread(
        storage.save_call_photo, request.app.state.gcs_client, normalized, identity,
        bytes(data), ctype,
    )
    lkapi = request.app.state.lkapi
    sid = await call_events.room_sid(lkapi, room_name)
    await call_events.log_server_event(
        normalized, sid, "photo", identity, name,
        data={
            "name": info["name"],
            "requested_by": (requested_by or "")[:128] or None,
            "size_bytes": info["size_bytes"],
            "width": width,
            "height": height,
        },
    )
    call_events.schedule_export(request.app.state.gcs_client, normalized, sid)
    return schemas.CallPhotoFile(**info)


@router.get("/v1/calls/{call_id}/photos/{filename}")
async def get_call_photo(call_id: str, filename: str, request: Request) -> Response:
    """Stable link to a call photo: redirects to a fresh signed URL. Auth: none,
    like the recordings list (anyone with the call link)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    ip = client_ip(request)
    if not await check_rate_limit(key=f"call-photo-get:ip:{ip}", limit=120, window_seconds=60):
        _error("rate_limit", "Too many requests", status.HTTP_429_TOO_MANY_REQUESTS)
    url = await asyncio.to_thread(
        storage.call_photo_url, request.app.state.gcs_client, normalized, f"photos/{filename}"
    )
    if url is None:
        _error("not_found", "No such photo", status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_302_FOUND, headers={
        "Location": str(url), "Cache-Control": "private, max-age=300",
    })


CLIENT_EVENT_TYPES = {
    "chat", "pointer", "mute", "media_toggle", "camera_switch", "device_switch",
    "stage_focus", "show_close", "leave", "data_packet", "camera_control", "torch",
}


@router.post("/v1/calls/{call_id}/events", status_code=status.HTTP_202_ACCEPTED)
async def post_call_events(
    call_id: str,
    body: schemas.CallEventBatch,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Interaction log from a participant's client (what *it* sent/did), stored
    with server receive timestamps and exported beside the recordings.
    Auth: this call's LiveKit token (identity/name come from the token)."""
    _require_enabled()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    identity, name = _call_claims(authorization, room_name)
    if not await check_rate_limit(key=f"call-events:{identity}", limit=120, window_seconds=60):
        _error("rate_limit", "Too many event batches", status.HTTP_429_TOO_MANY_REQUESTS)
    events = []
    for e in body.events:
        if e.type not in CLIENT_EVENT_TYPES:
            _error("invalid_event", f"Unknown event type: {e.type}")
        if len(json.dumps(e.data)) > 8192:
            _error("invalid_event", "Event data too large")
        events.append(e.model_dump())
    lkapi = request.app.state.lkapi
    sid = await call_events.room_sid(lkapi, room_name)
    stored = await call_events.record(
        normalized, sid, identity, name, events, sent_at=body.sent_at
    )
    call_events.schedule_export(request.app.state.gcs_client, normalized, sid)
    return {"stored": stored}


@router.post("/v1/calls/{call_id}/recording/start", response_model=schemas.CallRecordingState)
async def start_call_recording(
    call_id: str, request: Request, authorization: str | None = Header(default=None)
):
    """Start recording every participant's tracks. Auth: this call's LiveKit token."""
    _require_enabled()
    _require_recording_allowed()
    normalized = _normalize_call_id(call_id)
    room_name = open_call_room_name(normalized)
    identity, name = _call_claims(authorization, room_name)
    requested = datetime.now(UTC)  # recording takes effect from the request
    who = name or identity
    state = await call_recording.start(request.app.state.lkapi, normalized, room_name, who)
    sid = await call_events.room_sid(request.app.state.lkapi, room_name)
    await call_events.log_server_event(
        normalized, sid, "recording_start", identity, name, at=requested
    )
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
    identity, name = _call_claims(authorization, room_name)
    requested = datetime.now(UTC)
    await call_recording.stop(request.app.state.lkapi, room_name, name or identity)
    sid = await call_events.room_sid(request.app.state.lkapi, room_name)
    await call_events.log_server_event(
        normalized, sid, "recording_stop", identity, name, at=requested
    )
    # The recording just finished: write the interaction log beside it now.
    await call_events.export_now(request.app.state.gcs_client, normalized, sid)
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
