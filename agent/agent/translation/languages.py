"""Language codes for the translation bridge.

Focus languages: English, Mandarin, Spanish, Tagalog. Detection results from
STT providers are normalized onto these codes where possible; anything else is
carried through untranslated-code-wise but still tracked.
"""

from __future__ import annotations

import enum


class Language(str, enum.Enum):
    ENGLISH = "en"
    MANDARIN = "zh"
    SPANISH = "es"
    TAGALOG = "tl"


SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(lang.value for lang in Language)

# Aliases emitted by various STT providers -> our canonical codes.
_ALIASES = {
    "en-us": "en",
    "en_gb": "en",
    "en-gb": "en",
    "eng": "en",
    "english": "en",
    "cmn": "zh",
    "cmn-hans": "zh",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "zh_hans": "zh",
    "mandarin": "zh",
    "chinese": "zh",
    "spa": "es",
    "es-es": "es",
    "es-mx": "es",
    "es_mx": "es",
    "spanish": "es",
    "fil": "tl",  # ISO 639-2/3 for Filipino
    "phi": "tl",
    "filipino": "tl",
    "tagalog": "tl",
    "tag": "tl",
}


def normalize_language(code: str | None) -> str | None:
    """Map an STT-reported language code onto a canonical code, or pass through."""
    if not code:
        return None
    lowered = code.strip().lower().replace("_", "-")
    if lowered in SUPPORTED_LANGUAGES:
        return lowered
    return _ALIASES.get(lowered, lowered)


def is_supported(code: str | None) -> bool:
    return code in SUPPORTED_LANGUAGES
