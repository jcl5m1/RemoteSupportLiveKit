"""Translation via LiveKit Cloud Inference (LLM).

Short-utterance translation is a chat-completion task; a tight system prompt
keeps the model from adding pleasantries. One instance serves the whole room —
the core serializes per speaker, and translation calls are short.
"""

from __future__ import annotations

import logging

from livekit.agents import inference, llm

logger = logging.getLogger("translation.translator")

SYSTEM_PROMPT = (
    "You are a simultaneous interpreter on a live support call. Translate the "
    "user's message from {source} to {target}. Rules: output ONLY the "
    "translation, no quotes, no explanations, no prefatory text; keep it "
    "spoken-language natural; keep it the same length as the original; never "
    "add information that is not in the source."
)

DETECT_PROMPT = (
    "Identify the language of the user's message. Reply with exactly one "
    "ISO 639-1 code, chosen ONLY from: en, zh, es, tl. No other text."
)


class LiveKitTranslator:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._llm = inference.LLM(model=model, api_key=api_key, api_secret=api_secret)

    async def _complete(self, system: str, user: str) -> str:
        chat_ctx = llm.ChatContext()
        chat_ctx.add_message(role="system", content=[system])
        chat_ctx.add_message(role="user", content=[user])
        stream = self._llm.chat(chat_ctx=chat_ctx)
        parts = [chunk async for chunk in stream.to_str_iterable()]
        return "".join(parts).strip()

    async def translate(self, text: str, source: str, target: str) -> str:
        if not text.strip():
            return text
        try:
            return (await self._complete(SYSTEM_PROMPT.format(source=source, target=target), text)) or text
        except Exception:
            logger.exception("translation failed %s->%s; returning source text", source, target)
            return text

    async def detect_language(self, text: str) -> str | None:
        """Best-effort detection constrained to the focus languages."""
        try:
            code = (await self._complete(DETECT_PROMPT, text)).lower()[:2]
            return code if code in ("en", "zh", "es", "tl") else None
        except Exception:
            logger.exception("language detection failed")
            return None
