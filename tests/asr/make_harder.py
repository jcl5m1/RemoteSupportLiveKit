#!/usr/bin/env python3
"""Harden the benchmark.

1. Taglish (tl-en): 100 code-switched Tagalog/English support-call sentences,
   alternating a Tagalog and a Philippine-English edge-tts voice.
2. Phone condition: every clean clip gets a degraded copy -- babble (other
   speakers) + pink noise at 10 dB SNR, 300-3400 Hz narrowband, Opus 12 kbps
   VoIP -- in audio_phone/<lang>/<id>.opus.

Writes dataset_v2.csv: id, language, condition (clean|phone), text, audio_path.
  .venv/bin/python make_harder.py
"""

import asyncio
import csv
import random
import subprocess
from pathlib import Path

import numpy as np

SR = 16000
SNR_DB = 10.0
rng = random.Random(20261028)

# --- 1. Taglish -----------------------------------------------------------

OBJ = ["router", "laptop", "printer", "email account", "Wi-Fi", "password", "phone", "monitor"]
VERB = ["check", "restart", "update", "reset", "install"]
TIME = ["mamaya", "bukas ng umaga", "ngayon", "after lunch", "this afternoon"]
WHEN = ["kahapon", "last week", "this morning", "kanina"]
PROBLEM = ["ayaw pa rin mag-connect", "nag-e-error pa rin", "sobrang bagal", "hindi siya nag-o-on"]
TEMPLATES = [
    lambda: f"Pwede mo bang i-{rng.choice(VERB)} yung {rng.choice(OBJ)} {rng.choice(TIME)}?",
    lambda: f"Hindi gumagana yung {rng.choice(OBJ)} ko since {rng.choice(WHEN)}.",
    lambda: f"Na-try ko na i-{rng.choice(VERB)} yung {rng.choice(OBJ)} pero {rng.choice(PROBLEM)}.",
    lambda: f"Kailangan ko ng help sa {rng.choice(OBJ)} kasi {rng.choice(PROBLEM)}.",
    lambda: f"Okay na po, na-{rng.choice(VERB)} na yung {rng.choice(OBJ)} {rng.choice(WHEN)}.",
]
VOICES = ["fil-PH-BlessicaNeural", "en-PH-RosaNeural"]


def taglish_phrases(n: int = 100) -> list[str]:
    seen: list[str] = []
    while len(seen) < n:
        s = rng.choice(TEMPLATES)()
        if s not in seen:
            seen.append(s)
    return seen


async def synth_taglish() -> list[dict]:
    import edge_tts

    out = []
    sem = asyncio.Semaphore(3)

    async def one(i: int, text: str) -> dict:
        path = Path("audio/tl-en") / f"tl-en_{i:03d}.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            for attempt in range(5):
                try:
                    async with sem:
                        await edge_tts.Communicate(text, VOICES[i % 2]).save(str(path))
                    break
                except Exception:  # noqa: BLE001 - flaky service, retry
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                raise SystemExit(f"TTS failed: {text}")
        return {"id": f"tl-en_{i:03d}", "language": "tl-en", "text": text, "audio_path": str(path)}

    out = await asyncio.gather(*(one(i + 1, t) for i, t in enumerate(taglish_phrases())))
    return list(out)


# --- 2. Phone condition ---------------------------------------------------


def decode(path: str) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def pink(n: int, seed: int) -> np.ndarray:
    r = np.random.default_rng(seed)
    spec = np.fft.rfft(r.standard_normal(n))
    f = np.arange(len(spec))
    f[0] = 1
    x = np.fft.irfft(spec / np.sqrt(f), n)
    return x / (np.sqrt(np.mean(x**2)) + 1e-9)


def rms(x: np.ndarray) -> float:
    # Active-speech RMS: ignore near-silent frames.
    frames = x[: len(x) // 320 * 320].reshape(-1, 320)
    e = np.sqrt(np.mean(frames**2, axis=1))
    active = e[e > 0.1 * e.max()] if len(e) else e
    return float(np.sqrt(np.mean(active**2))) if len(active) else 1e-4


def degrade(row: dict, pool: list[str], idx: int) -> str:
    out = Path("audio_phone") / row["language"] / f"{row['id']}.opus"
    if out.exists():
        return str(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    speech = decode(row["audio_path"])
    n = len(speech)
    r = random.Random(idx)
    babble = np.zeros(n, np.float32)
    for p in r.sample([q for q in pool if q != row["audio_path"]], 4):
        b = decode(p)
        b = np.resize(b, n) if len(b) < n else b[:n]
        babble += np.roll(b, r.randrange(n)) / (rms(b) + 1e-9)
    babble /= np.sqrt(np.mean(babble**2)) + 1e-9
    noise = 0.7 * babble + 0.3 * pink(n, idx)
    noise *= rms(speech) / (np.sqrt(np.mean(noise**2)) + 1e-9) / (10 ** (SNR_DB / 20))
    mix = speech + noise
    mix /= max(1.0, float(np.abs(mix).max()) / 0.95)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
         "-af", "highpass=f=300,lowpass=f=3400,aresample=8000",
         "-c:a", "libopus", "-b:a", "12k", "-application", "voip", str(out)],
        input=mix.astype(np.float32).tobytes(), check=True,
    )
    return str(out)


def main() -> None:
    base = list(csv.DictReader(open("dataset.csv", encoding="utf-8")))
    base = [r for r in base if r["language"] != "test"]
    taglish = asyncio.run(synth_taglish())
    clean = base + taglish
    pool = [r["audio_path"] for r in clean]
    rows = []
    for i, r in enumerate(clean):
        rows.append({**r, "condition": "clean"})
        rows.append({**r, "condition": "phone", "audio_path": degrade(r, pool, i)})
    with open("dataset_v2.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "language", "condition", "text", "audio_path"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} rows ({len(clean)} clean + {len(clean)} phone)")


if __name__ == "__main__":
    main()
