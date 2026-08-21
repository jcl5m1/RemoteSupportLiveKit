"""Pydantic request/response models. Contract: docs/04-api-contract.md."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from .models import AgentMode, ParticipantRole, RecordingKind, RecordingState, UtteranceSource

# --- shared ------------------------------------------------------------


class LiveKitCredentials(BaseModel):
    ws_url: str
    token: str
    room_name: str
    identity: str
    expires_at: datetime


# --- session creation --------------------------------------------------


class CreateSessionRequest(BaseModel):
    device_id: str
    display_name: str | None = None
    locale: str = "en-US"


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID
    join_code: str
    join_code_expires_at: datetime
    deep_link: str
    universal_link: str
    qr_payload: str
    caller_session_token: str
    consent_required: bool = True
    consent_text_version: str
    consent_text: str
    # Note: no LiveKit credentials here. See docs/04 and FR-7.1.


class ConsentRequest(BaseModel):
    accepted: bool
    consent_text_version: str


class ConsentResponse(BaseModel):
    accepted: bool
    session_state: str
    livekit: LiveKitCredentials | None = None
    recording_enabled: bool = True


class JoinRequest(BaseModel):
    join_code: str
    display_name: str | None = None


class JoinResponse(BaseModel):
    session_id: uuid.UUID
    livekit: LiveKitCredentials
    ai_enabled: bool
    agent_mode: AgentMode
    recording_enabled: bool
    caller_display_name: str | None = None


# --- session detail ----------------------------------------------------


class ParticipantInfo(BaseModel):
    role: ParticipantRole
    identity: str
    display_name: str | None = None
    joined_at: datetime | None = None
    left_at: datetime | None = None


class RecordingInfo(BaseModel):
    kind: RecordingKind
    role: ParticipantRole | None = None
    state: RecordingState
    mime_type: str | None = None
    duration_ms: int | None = None
    size_bytes: int | None = None
    gcs_uri: str | None = None
    download_url: str | None = None
    url_expires_at: datetime | None = None


class SessionDetail(BaseModel):
    session_id: uuid.UUID
    state: str
    room_name: str
    caller_identity: str | None = None
    support_identity: str | None = None
    ai_enabled: bool
    agent_mode: AgentMode
    recording_enabled: bool
    metadata_version: int
    participants: list[ParticipantInfo]
    recordings: list[RecordingInfo]
    started_at: datetime | None = None
    ended_at: datetime | None = None


# --- agent control -----------------------------------------------------


class AgentToggleRequest(BaseModel):
    enabled: bool
    reason: str | None = None


class AgentToggleResponse(BaseModel):
    ai_enabled: bool
    metadata_version: int
    applied_at: datetime


class AgentModeRequest(BaseModel):
    mode: AgentMode


# --- transcripts -------------------------------------------------------


class UtteranceIn(BaseModel):
    client_utterance_id: str
    role: ParticipantRole
    identity: str
    source: UtteranceSource
    start_ms: int
    end_ms: int | None = None
    text: str
    language: str | None = None
    confidence: float | None = None
    agent_mode: AgentMode | None = None
    ai_enabled: bool | None = None


class UtteranceIngestRequest(BaseModel):
    utterances: list[UtteranceIn] = Field(max_length=200)


class UtteranceIngestResponse(BaseModel):
    accepted: int
    duplicates: int


class UtteranceOut(UtteranceIn):
    seq: int


class TranscriptPage(BaseModel):
    utterances: list[UtteranceOut]
    next_cursor: str | None = None


class TranscriptExportUrls(BaseModel):
    jsonl_url: str
    vtt_url: str
    txt_url: str


class RecordingsResponse(BaseModel):
    recordings: list[RecordingInfo]
    transcript: TranscriptExportUrls | None = None
