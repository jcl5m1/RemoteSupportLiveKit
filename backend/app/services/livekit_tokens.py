"""LiveKit access-token minting.

Grants per role are specified in docs/02-architecture.md. Two rules that must
not be relaxed:

  * No client ever receives ``room_admin``. Room metadata carries the AI toggle
    and the recording indicator; a client that could rewrite it could lie to the
    other participant.
  * No client receives ``can_update_own_metadata``. Roles are server-assigned.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from livekit import api

from ..config import get_settings
from ..models import ParticipantRole

settings = get_settings()

CALLER_PREFIX = "caller-"
SUPPORT_PREFIX = "support-"
AGENT_IDENTITY = "agent"


def room_name_for(session_id: uuid.UUID) -> str:
    return f"rs_{session_id}"


def make_caller_identity() -> str:
    return f"{CALLER_PREFIX}{uuid.uuid4().hex[:8]}"


def make_support_identity(user_id: str) -> str:
    return f"{SUPPORT_PREFIX}{user_id}"


def role_from_identity(identity: str) -> ParticipantRole | None:
    if identity == AGENT_IDENTITY:
        return ParticipantRole.AGENT
    if identity.startswith(CALLER_PREFIX):
        return ParticipantRole.CALLER
    if identity.startswith(SUPPORT_PREFIX):
        return ParticipantRole.SUPPORT
    return None


def build_token(
    *,
    room_name: str,
    identity: str,
    role: ParticipantRole,
    display_name: str | None,
    session_id: uuid.UUID,
    ttl_seconds: int,
) -> str:
    """Build a scoped LiveKit room token.

    The signed metadata is the authoritative role claim. No client ever receives
    room_admin or can_update_own_metadata.
    """
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(display_name or "")
        .with_metadata(
            json.dumps({"role": role.value, "session_id": str(session_id)})
        )
        .with_attributes(
            {"role": role.value, "display_name": display_name or ""}
        )
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
                can_update_own_metadata=False,
                room_admin=False,
            )
        )
        .with_ttl(timedelta(seconds=ttl_seconds))
        .to_jwt()
    )
