#!/usr/bin/env python3
"""Pre-generate edge-tts audio used by tests/test_translation_loopback.py.

Microsoft's edge-tts service returns no audio for the Tagalog voice when
called from GitHub Actions runners, so those clips are checked in here and the
test harness uses them instead of the network. File names hash (voice, text),
so changing a sentence or voice regenerates rather than reusing stale audio.

  cd agent && .venv/bin/python tests/fixtures/tts/generate.py [lang ...]   # default: tl
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # agent/ (for agent.* and tests.*)

from agent.translation.synthesizer import EDGE_VOICES
from tests.translation_corpus import SENTENCES


def fixture_path(language: str, voice: str, text: str) -> Path:
    h = hashlib.sha256(f"{voice}||{text}".encode()).hexdigest()[:16]
    return HERE / f"{language}-{h}.mp3"


async def main(languages: list[str]) -> None:
    import edge_tts

    for language in languages:
        voice = EDGE_VOICES[language]
        for text in SENTENCES[language]:
            path = fixture_path(language, voice, text)
            if path.exists():
                continue
            for attempt in range(5):
                try:
                    await edge_tts.Communicate(text, voice).save(str(path))
                    break
                except Exception as exc:  # noqa: BLE001 - retry flaky service
                    print(f"retry {attempt + 1}: {exc}", file=sys.stderr)
                    await asyncio.sleep(2)
            else:
                raise SystemExit(f"could not synthesize {language}: {text!r}")
            print(path.name, path.stat().st_size)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["tl"]))
