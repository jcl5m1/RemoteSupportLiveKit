"""Transcript ingest and export. See docs/06-recording-transcripts.md.

Ingest is idempotent on (session_id, client_utterance_id) so the agent's
buffered sink can retry a batch after a network blip for free.

Export writes four objects next to the media and is safe to re-run: it
overwrites the same paths, which is the intended recovery path for a
late-arriving utterance batch.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from google.cloud import storage as gcs
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .. import schemas
from ..config import get_settings
from ..models import (
    AgentEvent,
    Recording,
    Session,
    SessionParticipant,
    TranscriptUtterance,
)
from . import storage

settings = get_settings()

_SPEAKER_LABEL = {
    "caller": "Caller",
    "support": "Support",
    "agent": "Assistant",
}


async def ingest_utterances(
    db, session_id: uuid.UUID, utterances: list[schemas.UtteranceIn]
) -> dict[str, int]:
    """Bulk INSERT ... ON CONFLICT DO NOTHING.

    Returns {"accepted": n, "duplicates": m}.
    """
    if not utterances:
        return {"accepted": 0, "duplicates": 0}

    values = [
        {
            "session_id": session_id,
            "client_utterance_id": u.client_utterance_id,
            "role": u.role,
            "identity": u.identity,
            "source": u.source,
            "start_ms": u.start_ms,
            "end_ms": u.end_ms,
            "text": u.text,
            "language": u.language,
            "confidence": u.confidence,
            "agent_mode": u.agent_mode,
            "ai_enabled": u.ai_enabled,
        }
        for u in utterances
    ]

    stmt = (
        insert(TranscriptUtterance)
        .values(values)
        .on_conflict_do_nothing(index_elements=["session_id", "client_utterance_id"])
        .returning(TranscriptUtterance.id)
    )
    result = await db.execute(stmt)
    accepted = len(result.scalars().all())
    return {"accepted": accepted, "duplicates": len(utterances) - accepted}


async def export_transcript(
    db, gcs_client: gcs.Client, session: Session
) -> dict[str, str]:
    """Write the four export objects, return their gs:// URIs.

    sessions/{id}/transcript/transcript.jsonl   canonical, one object per line
    sessions/{id}/transcript/transcript.vtt     WebVTT, <v Caller|Support|Assistant>
    sessions/{id}/transcript/transcript.txt     "[mm:ss] SPEAKER: text"
    sessions/{id}/session.json                  header + mode/AI timelines + media manifest

    Finally set sessions.transcript_exported_at.
    """
    utterances_result = await db.execute(
        select(TranscriptUtterance)
        .where(TranscriptUtterance.session_id == session.id)
        .order_by(TranscriptUtterance.start_ms, TranscriptUtterance.id)
    )
    utterances = utterances_result.scalars().all()

    recordings_result = await db.execute(
        select(Recording).where(Recording.session_id == session.id)
    )
    recordings = recordings_result.scalars().all()

    agent_events_result = await db.execute(
        select(AgentEvent)
        .where(AgentEvent.session_id == session.id)
        .where(AgentEvent.event_type.in_(["mode_changed", "ai_enabled_changed"]))
        .order_by(AgentEvent.occurred_at)
    )
    agent_events = agent_events_result.scalars().all()

    participants_result = await db.execute(
        select(SessionParticipant).where(SessionParticipant.session_id == session.id)
    )
    participants = participants_result.scalars().all()

    jsonl_text = _build_jsonl(utterances)
    vtt_text = _build_vtt(utterances)
    txt_text = _build_txt(utterances)
    session_json = _build_session_json(
        session, recordings, agent_events, utterances, participants
    )

    base_prefix = f"sessions/{session.id}/"
    paths = {
        "jsonl": base_prefix + "transcript/transcript.jsonl",
        "vtt": base_prefix + "transcript/transcript.vtt",
        "txt": base_prefix + "transcript/transcript.txt",
        "session": base_prefix + "session.json",
    }

    bucket = gcs_client.bucket(settings.gcs_bucket)
    bucket.blob(paths["jsonl"]).upload_from_string(
        jsonl_text, content_type="application/jsonlines+json"
    )
    bucket.blob(paths["vtt"]).upload_from_string(vtt_text, content_type="text/vtt")
    bucket.blob(paths["txt"]).upload_from_string(txt_text, content_type="text/plain")
    bucket.blob(paths["session"]).upload_from_string(
        json.dumps(session_json, indent=2, default=_json_default),
        content_type="application/json",
    )

    session.transcript_exported_at = datetime.now(UTC)
    await db.commit()

    return {
        "jsonl": storage.gcs_uri(paths["jsonl"]),
        "vtt": storage.gcs_uri(paths["vtt"]),
        "txt": storage.gcs_uri(paths["txt"]),
        "session": storage.gcs_uri(paths["session"]),
    }


def _build_jsonl(utterances) -> str:
    lines = []
    for seq, u in enumerate(utterances, start=1):
        obj = {
            "seq": seq,
            "role": u.role.value,
            "identity": u.identity,
            "source": u.source.value,
            "start_ms": u.start_ms,
            "end_ms": u.end_ms,
            "text": u.text,
            "agent_mode": u.agent_mode.value if u.agent_mode else None,
            "ai_enabled": u.ai_enabled,
        }
        if u.language is not None:
            obj["language"] = u.language
        if u.confidence is not None:
            obj["confidence"] = u.confidence
        lines.append(json.dumps(obj, default=_json_default))
    return "\n".join(lines) + ("\n" if lines else "")


def _build_vtt(utterances) -> str:
    cues = ["WEBVTT", ""]
    for u in utterances:
        cues.append(
            format_vtt_cue(
                u.start_ms,
                u.end_ms,
                u.role.value,
                u.text,
            )
        )
        cues.append("")
    return "\n".join(cues)


def _build_txt(utterances) -> str:
    lines = []
    for u in utterances:
        speaker = _SPEAKER_LABEL.get(u.role.value, u.role.value.upper())
        lines.append(f"[{_txt_ts(u.start_ms)}] {speaker}: {u.text}")
    return "\n".join(lines) + ("\n" if lines else "")


def _build_session_json(
    session, recordings, agent_events, utterances, participants
) -> dict:
    mode_timeline = [
        {
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "actor": e.actor,
            "mode": e.payload.get("new"),
        }
        for e in agent_events
        if e.event_type == "mode_changed"
    ]
    ai_timeline = [
        {
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            "actor": e.actor,
            "ai_enabled": e.payload.get("new"),
        }
        for e in agent_events
        if e.event_type == "ai_enabled_changed"
    ]

    media_manifest = [
        {
            "kind": r.kind.value,
            "role": r.role.value if r.role else None,
            "state": r.state.value,
            "gcs_uri": r.gcs_uri,
            "duration_ms": r.duration_ms,
            "size_bytes": r.size_bytes,
            "mime_type": r.mime_type,
        }
        for r in recordings
    ]

    return {
        "session_id": str(session.id),
        "room_name": session.room_name,
        "state": session.state.value if session.state else None,
        "ai_enabled": session.ai_enabled,
        "agent_mode": session.agent_mode.value if session.agent_mode else None,
        "recording_enabled": session.recording_enabled,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "transcript_exported_at": (
            session.transcript_exported_at.isoformat()
            if session.transcript_exported_at
            else None
        ),
        "utterance_count": len(utterances),
        "participants": [
            {
                "role": p.role.value,
                "identity": p.identity,
                "display_name": p.display_name,
                "joined_at": p.joined_at.isoformat() if p.joined_at else None,
                "left_at": p.left_at.isoformat() if p.left_at else None,
            }
            for p in participants
        ],
        "mode_timeline": mode_timeline,
        "ai_timeline": ai_timeline,
        "media_manifest": media_manifest,
    }


def format_vtt_cue(start_ms: int, end_ms: int | None, role: str, text: str) -> str:
    """WebVTT cue. Voice spans let a player colour speakers automatically."""
    speaker = _SPEAKER_LABEL[role]
    end = end_ms if end_ms is not None else start_ms + 2000
    return f"{_ts(start_ms)} --> {_ts(end)}\n<v {speaker}>{text}"


def _ts(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"


def _txt_ts(ms: int) -> str:
    m, rem = divmod(ms, 60_000)
    s = rem // 1000
    return f"{m:02d}:{s:02d}"


def _json_default(obj: object):
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
