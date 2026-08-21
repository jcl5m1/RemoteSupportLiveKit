"""Agent worker settings.

Every model is a ``provider/model`` identifier resolved through **LiveKit Cloud
Inference**, authenticated with ``LIVEKIT_API_KEY``. There are deliberately no
per-provider API keys: one vendor relationship, one bill (NFR-6).

Catalog verified 2026-08 against docs.livekit.io/agents/models/inference/.
Anthropic/Claude is **not** in the catalog -- an ``anthropic/*`` identifier will
not resolve. Re-verify before changing any of these; the catalog moves faster
than any doc.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    agent_name: str = "support-agent"

    # Backend
    backend_url: str = "http://localhost:8000"
    service_api_key: str

    # Models — all via LiveKit Cloud Inference
    stt_model: str = "deepgram/nova-3:en"
    # Kimi K2.6, served via Baseten. Retired ids that will NOT resolve:
    # moonshotai/kimi-k2-instruct, moonshotai/kimi-k2.5.
    llm_model: str = "moonshotai/kimi-k2.6"
    # LiveKit Inference TTS format is ``provider/model:voice_id``.  The old
    # ``:en`` language suffix is not a valid voice id and causes the synthesizer
    # to emit no audio frames.  ``deepgram/aura-2:athena`` is the documented
    # sample voice for Deepgram Aura-2.
    tts_model: str = "deepgram/aura-2:athena"

    # Fallback local tone generator.  LiveKit Cloud Inference TTS is failing in
    # this project (``no audio frames were pushed`` across providers/models), so
    # the agent uses a simple sine-wave TTS shim by default for headless
    # regression and to keep the speech scheduler moving.  Set this to false to
    # try LiveKit Inference TTS once the cloud issue is resolved.
    use_dummy_tts: bool = True
    # The support stream uses the same model identifier but a SEPARATE STT
    # instance -- sharing one instance across two streams is not safe.
    support_stt_model: str | None = None

    @property
    def effective_support_stt_model(self) -> str:
        return self.support_stt_model or self.stt_model

    # Behaviour
    agent_wake_phrases: str = "hey assistant,hey agent,hey ai"
    heartbeat_seconds: int = 30

    # --- Local development -------------------------------------------------
    # When true and LiveKit credentials are missing, the worker keeps the
    # container alive without connecting to LiveKit.
    allow_degraded_start: bool = False

    log_level: str = "INFO"

    @property
    def wake_phrases(self) -> tuple[str, ...]:
        return tuple(p.strip().lower() for p in self.agent_wake_phrases.split(",") if p.strip())


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()  # type: ignore[call-arg]
