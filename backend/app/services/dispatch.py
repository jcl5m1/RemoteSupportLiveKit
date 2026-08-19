"""Explicit agent dispatch and room metadata updates."""

from __future__ import annotations

import json

from livekit import api

from ..config import get_settings

settings = get_settings()


async def dispatch_agent(lkapi: api.LiveKitAPI, session) -> str:
    """Dispatch the named agent into the session's room.

    ``caller_identity`` is the critical field: the worker feeds it straight into
    ``RoomOptions.participant_identity``, which binds the agent's input pipeline
    to the caller alone (docs/05).
    """
    if not session.caller_identity:
        raise ValueError("Session has no caller_identity; cannot dispatch agent")

    dispatch = await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=settings.livekit_agent_name,
            room=session.room_name,
            metadata=json.dumps({
                "session_id": str(session.id),
                "caller_identity": session.caller_identity,
                "ai_enabled": session.ai_enabled,
                "agent_mode": session.agent_mode.value,
                "backend_url": settings.self_url if hasattr(settings, "self_url") else "",
            }),
        )
    )
    return dispatch.id


async def update_room_metadata(lkapi: api.LiveKitAPI, session) -> None:
    """Push control-plane state into room metadata.

    ``v`` must already be incremented by the caller. Receivers drop any metadata
    whose ``v`` is not greater than the last applied, preventing stale toggles.
    """
    payload = {
        "session_id": str(session.id),
        "ai_enabled": session.ai_enabled,
        "recording": session.recording_enabled,
        "mode": session.agent_mode.value,
        "v": session.metadata_version,
    }
    await lkapi.room.update_room_metadata(
        api.UpdateRoomMetadataRequest(
            room=session.room_name, metadata=json.dumps(payload)
        )
    )
