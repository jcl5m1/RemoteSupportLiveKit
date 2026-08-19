"""Tests for LiveKit token minting."""

from __future__ import annotations

import json
import uuid

import jwt

from app.models import ParticipantRole
from app.services.livekit_tokens import (
    build_token,
    make_caller_identity,
    make_support_identity,
    role_from_identity,
    room_name_for,
)


def test_room_name_format():
    sid = uuid.uuid4()
    assert room_name_for(sid) == f"rs_{sid}"


def test_identity_prefixes():
    caller = make_caller_identity()
    support = make_support_identity("u_123")
    assert caller.startswith("caller-")
    assert support.startswith("support-")


def test_role_from_identity():
    assert role_from_identity("caller-abc123") == ParticipantRole.CALLER
    assert role_from_identity("support-u_1") == ParticipantRole.SUPPORT
    assert role_from_identity("agent") == ParticipantRole.AGENT
    assert role_from_identity("random") is None


def test_token_contains_role_and_session():
    sid = uuid.uuid4()
    token = build_token(
        room_name="rs_test",
        identity="caller-abc",
        role=ParticipantRole.CALLER,
        display_name="Sam",
        session_id=sid,
        ttl_seconds=60,
    )
    payload = jwt.decode(token, options={"verify_signature": False})
    metadata = payload.get("metadata")
    assert metadata is not None
    meta = json.loads(metadata)
    assert meta["role"] == "caller"
    assert meta["session_id"] == str(sid)
    assert payload["video"]["room"] == "rs_test"
    assert payload["video"]["roomJoin"] is True
    assert payload["video"].get("roomAdmin") is not True
    assert payload["video"].get("canUpdateOwnMetadata") is not True


def test_support_token_does_not_get_admin():
    token = build_token(
        room_name="rs_test",
        identity="support-u_1",
        role=ParticipantRole.SUPPORT,
        display_name="Dana",
        session_id=uuid.uuid4(),
        ttl_seconds=60,
    )
    payload = jwt.decode(token, options={"verify_signature": False})
    video = payload.get("video", {})
    assert video.get("roomAdmin") is not True
    assert video.get("canUpdateOwnMetadata") is not True
