"""Per-job session state. The worker is stateless (NFR-5); this is a cache of
what lives in Postgres, reconciled on start and on the 30s heartbeat.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from livekit.agents import llm


class AgentMode(str, enum.Enum):
    SOLO = "SOLO"
    ASSISTED = "ASSISTED"
    WRAP_UP = "WRAP_UP"


class StateLoadError(Exception):
    """Raised when the worker cannot hydrate state from the backend."""


@dataclass
class SessionState:
    session_id: str
    caller_identity: str
    support_identity: str | None = None
    mode: AgentMode = AgentMode.SOLO
    ai_enabled: bool = True
    metadata_version: int = 0
    # Zero point for every transcript offset. Comes from the backend so it
    # matches the value the recordings are aligned against.
    started_at: datetime | None = None
    chat_context: llm.ChatContext = field(default_factory=llm.ChatContext)
    _http: httpx.AsyncClient | None = field(default=None, repr=False)

    @classmethod
    async def load(cls, session_id: str, settings) -> SessionState:
        """GET /v1/sessions/{id} with the service key and hydrate.

        On a mid-call worker restart, also replay
        GET /v1/sessions/{id}/transcript into the chat context as tagged turns.

        The replay will not be a byte-perfect reconstruction of the prior LLM
        context, and that is fine -- the transcript is the durable artifact,
        the context is a cache.
        """
        client = httpx.AsyncClient(
            base_url=settings.backend_url.rstrip("/"),
            headers={"X-Service-Key": settings.service_api_key},
            timeout=30.0,
        )
        try:
            r = await client.get(f"/v1/sessions/{session_id}")
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise StateLoadError(f"Failed to load session {session_id}: {exc}") from exc

        participants = data.get("participants", [])
        support_identity = next(
            (p["identity"] for p in participants if p.get("role") == "support"),
            None,
        )
        caller_identity = data.get("caller_identity") or next(
            (p["identity"] for p in participants if p.get("role") == "caller"),
            None,
        )
        if not caller_identity:
            raise StateLoadError(f"Session {session_id} has no caller identity")

        started_at_raw = data.get("started_at")
        started_at = datetime.fromisoformat(started_at_raw) if started_at_raw else None

        mode = AgentMode(data.get("agent_mode", "SOLO"))
        state = cls(
            session_id=session_id,
            caller_identity=caller_identity,
            support_identity=support_identity,
            mode=mode,
            ai_enabled=data.get("ai_enabled", True),
            metadata_version=data.get("metadata_version", 0),
            started_at=started_at,
            _http=client,
        )
        await state._replay_transcript()
        return state

    async def _replay_transcript(self) -> None:
        """Fetch prior finals and reconstruct a read-only chat context."""
        if self._http is None:
            return
        try:
            r = await self._http.get(
                f"/v1/sessions/{self.session_id}/transcript",
                params={"since_ms": 0, "limit": 1000},
            )
            r.raise_for_status()
        except httpx.HTTPError:
            # A transient backend error should not prevent the agent from joining;
            # it will simply start with an empty context.
            return

        chat_ctx = llm.ChatContext()
        for u in r.json().get("utterances", []):
            role = "assistant" if u.get("source") == "agent_llm" else "user"
            text = u.get("text", "")
            if u.get("role") == "support":
                text = f"[SUPPORT] {text}"
            chat_ctx.add_message(llm.ChatMessage(role=role, content=[text]))
        self.chat_context = chat_ctx

    def now_offset_ms(self) -> int:
        """Milliseconds since session start."""
        if self.started_at is None:
            return 0
        return int((datetime.now(UTC) - self.started_at).total_seconds() * 1000)

    async def report_mode(self) -> None:
        """POST /v1/sessions/{id}/agent/mode."""
        if self._http is None:
            return
        try:
            r = await self._http.post(
                f"/v1/sessions/{self.session_id}/agent/mode",
                json={"mode": self.mode.value},
            )
            r.raise_for_status()
            data = r.json()
            self.metadata_version = max(
                self.metadata_version,
                data.get("metadata_version", self.metadata_version),
            )
        except httpx.HTTPError:
            # The agent keeps working; the heartbeat will reconcile.
            pass
