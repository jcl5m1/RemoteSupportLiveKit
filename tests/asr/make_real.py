#!/usr/bin/env python3
"""Add real recorded speech to the benchmark ("real" condition).

- Google FLEURS test split (CC-BY-4.0, people reading sentences):
  en_us, es_419, fil_ph (Tagalog), cmn_hans_cn (mainland Mandarin).
- ryL/Taiwan-mandarin test split (Taiwan TV drama dialogue, subtitle-style
  transcripts; licence not stated -- used for internal evaluation only).
No free Taglish speech set exists, so Taglish stays synthetic.

100 clips per set (seeded), up to 20 s (FLEURS) / 30 s (drama), written as
16 kHz WAV under <bench>/wav/real/<lang>/ and appended to <bench>/manifest.csv.
Audio is not committed (licences / size).

  .venv/bin/python make_real.py --bench /tmp/asr-bench   # parquet files in <bench>/real/
"""

import argparse
import csv
import io
import random
import subprocess
import wave
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SETS = [  # (parquet, language, id prefix, text column, max seconds)
    ("fleurs-en_us.parquet", "en", "fleurs_en", "raw_transcription", 20),
    ("fleurs-es_419.parquet", "es", "fleurs_es", "raw_transcription", 20),
    ("fleurs-fil_ph.parquet", "tl", "fleurs_tl", "raw_transcription", 20),
    ("fleurs-cmn_hans_cn.parquet", "zh", "fleurs_zh", "raw_transcription", 20),
    ("ryl-test0.parquet", "zh-tw", "drama_zhtw", "transcription", 30),
]
N = 100


def to_wav16k(data: bytes, out: Path) -> np.ndarray:
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         input=data, capture_output=True, check=True).stdout
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return np.frombuffer(pcm, np.int16).astype(np.float32)


def speech_end(x: np.ndarray) -> float:
    fr = x[: len(x) // 160 * 160].reshape(-1, 160)
    e = np.sqrt((fr**2).mean(1))
    idx = np.nonzero(e > max(e.max() * 0.1, 50))[0]
    return round((idx[-1] + 1) * 0.01, 2) if len(idx) else round(len(x) / 16000, 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="/tmp/asr-bench")
    args = ap.parse_args()
    bench = Path(args.bench)
    manifest = bench / "manifest.csv"
    rows = [r for r in csv.DictReader(open(manifest, encoding="utf-8")) if r["condition"] != "real"]
    fields = list(rows[0])
    added = 0
    for fname, lang, prefix, col, max_s in SETS:
        t = pq.read_table(bench / "real" / fname).to_pylist()
        rng = random.Random(f"{prefix}-20261028")
        rng.shuffle(t)
        n = 0
        seen = set()
        for r in t:
            text = str(r[col]).strip().strip('"').strip()
            if not text or text in seen:
                continue
            out = bench / "wav" / "real" / lang / f"{prefix}_{n + 1:03d}.wav"
            x = to_wav16k(r["audio"]["bytes"], out)
            dur = len(x) / 16000
            if dur > max_s or dur < 1.5:
                out.unlink()
                continue
            seen.add(text)
            n += 1
            rows.append({"id": f"{prefix}_{n:03d}", "language": lang, "condition": "real", "text": text,
                         "wav": str(out.relative_to(bench)), "speech_end_s": speech_end(x), "audio_s": round(dur, 2)})
            if n == N:
                break
        added += n
        print(f"{prefix}: {n} clips")
    with open(manifest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"manifest: {len(rows)} rows (+{added} real)")


if __name__ == "__main__":
    main()
