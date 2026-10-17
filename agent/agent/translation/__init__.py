"""Real-time multilingual translation bridge for audio-only LiveKit rooms.

Peers join with microphone only (no camera required). Every utterance is
transcribed, language-detected, translated, and spoken back to every OTHER
participant either in English (``ENGLISH_RELAY``) or in each listener's own
detected/declared language (``NATIVE_RELAY``), switchable at runtime.

Layering:

    bridge.py     LiveKit glue: track subscription, per-participant STT pumps,
                  per-listener audio tracks, chat data packets, mode control.
    core.py       Pure decision logic: who hears what, in which language.
                  Fully testable without a room.
    registry.py   Participant registry; the authoritative per-user language map.
    translator.py Translator protocol + LiveKit Cloud Inference implementation.
    synthesizer.py TTS protocol + LiveKit / edge-tts / macOS `say` implementations.
"""

from .core import Delivery, RelayMode, TranslationCore, TranslationEvent
from .languages import SUPPORTED_LANGUAGES, Language, normalize_language
from .registry import Participant, ParticipantRegistry

__all__ = [
    "SUPPORTED_LANGUAGES",
    "Delivery",
    "Language",
    "Participant",
    "ParticipantRegistry",
    "RelayMode",
    "TranslationCore",
    "TranslationEvent",
    "normalize_language",
]
