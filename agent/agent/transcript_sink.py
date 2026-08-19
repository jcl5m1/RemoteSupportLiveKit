"""Buffered transcript writer -- the single owner of all three sources.

    caller speech  <- session.on("user_input_transcribed"), is_final only
    agent speech   <- session.on("conversation_item_added"), role == assistant
    support speech <- SupportTranscriber._on_final

Every emit is stamped with a ULID, the current mode, ai_enabled, and start_ms
measured from sessions.started_at. Batches on a 2s timer or 20 items and POSTs
to /v1/sessions/{id}/utterances, which is idempotent -- so retries are free.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from ulid import ULID

logger = logging.getLogger("support-agent.sink")

BATCH_SIZE = 20
BATCH_INTERVAL_SECONDS = 2.0
MAX_RETRIES = 5


class TranscriptSink:
    def __init__(self, *, state, settings) -> None:
        self._state = state
        self._settings = settings
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._closed = False

    def emit(
        self,
        *,
        role: str,
        identity: str,
        source: str,
        text: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        language: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Stamp and buffer an utterance for durable ingest."""
        if self._closed:
            logger.warning("emit called on closed sink; dropping utterance")
            return
        self._buffer.append(
            {
                "client_utterance_id": str(ULID()),
                "role": role,
                "identity": identity,
                "source": source,
                "start_ms": start_ms if start_ms is not None else self._state.now_offset_ms(),
                "end_ms": end_ms,
                "text": text,
                "language": language,
                "confidence": confidence,
                "agent_mode": self._state.mode.value,
                "ai_enabled": self._state.ai_enabled,
            }
        )
        if len(self._buffer) >= BATCH_SIZE:
            asyncio.create_task(self.flush())
        elif self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._timer_flush())

    async def _timer_flush(self) -> None:
        await asyncio.sleep(BATCH_INTERVAL_SECONDS)
        await self.flush()

    async def flush(self) -> None:
        """POST the buffered batch with exponential backoff.

        Never drop utterances on failure -- keep them buffered and retry. The
        endpoint dedupes on (session_id, client_utterance_id).
        """
        if self._state._http is None:
            return
        async with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:]

        for attempt in range(MAX_RETRIES):
            try:
                r = await self._state._http.post(
                    f"/v1/sessions/{self._state.session_id}/utterances",
                    json={"utterances": batch},
                    timeout=30.0,
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                wait = 2**attempt
                logger.warning(
                    "utterance batch failed (attempt %d/%d): %s; retrying in %ss",
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            async with self._lock:
                # Remove only the items that were successfully posted.
                posted_ids = {u["client_utterance_id"] for u in batch}
                self._buffer = [
                    u for u in self._buffer if u["client_utterance_id"] not in posted_ids
                ]
            return

        logger.error("utterance batch exhausted retries; %d items remain buffered", len(batch))

    async def flush_and_close(self) -> None:
        """Registered via ctx.add_shutdown_callback. Last chance to persist."""
        self._closed = True
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
