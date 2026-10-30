#!/usr/bin/env python3
"""Cloud streaming STT through LiveKit Inference (same path as the agent).

Each clip is streamed in real time (20 ms frames) followed by 2 s of silence;
latency = time from the end of the speech (manifest speech_end_s) to the last
final transcript, i.e.
what a caller waits after they stop talking. Run with the agent venv and its
LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET:

  agent/.venv/bin/python tests/asr/bench_cloud.py --model deepgram/nova-3 \
      --manifest /tmp/asr-bench/manifest.csv --out /tmp/asr-bench/results
"""

import argparse
import asyncio
import csv
import json
import os
import time
import wave
from pathlib import Path

import aiohttp
import numpy as np
from livekit import rtc
from livekit.agents import inference, stt as agents_stt

FRAME_MS = 20
TAIL_S = 2.0
WAIT_AFTER_S = 8.0

# Benchmark language -> provider language code; "multi" = auto/multilingual.
LANG = {"en": "en", "es": "es", "tl": "tl", "tl-en": "multi", "zh": "zh", "zh-tw": "zh-TW"}
# Providers that detect the language themselves (as the agent uses them).
AUTO_LANGUAGE = ("assemblyai/", "elevenlabs/", "deepgram/flux-general-multi")


def _norm(t: str) -> str:
    return "".join(ch for ch in t.lower() if ch.isalnum())


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), np.int16)


async def run_clip(model: str, lang: str | None, row: dict, base: Path, session: aiohttp.ClientSession) -> dict:
    kwargs = {"model": model, "http_session": session}
    if not model.startswith(AUTO_LANGUAGE) and lang:
        kwargs["language"] = lang
    s = inference.STT(**kwargs)
    pcm = load_wav(str(base / row["wav"]))
    spf = 16000 * FRAME_MS // 1000
    finals: list[str] = []
    last_final = None
    err = None
    stream = s.stream()

    async def reader() -> None:
        nonlocal last_final
        async for ev in stream:
            if ev.type == agents_stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                t = ev.alternatives[0].text.strip()
                if t:
                    # Some providers re-send a turn's final, formatted: replace it.
                    if finals and _norm(t) == _norm(finals[-1]):
                        finals[-1] = t
                    else:
                        finals.append(t)
                    last_final = time.monotonic()

    rtask = asyncio.create_task(reader())
    start = time.monotonic()
    try:
        silence = np.zeros(int(16000 * TAIL_S), np.int16)
        audio = np.concatenate([pcm, silence])
        # End of the actual speech (clips carry trailing silence / noise).
        speech_end = start + float(row["speech_end_s"])
        for i in range(0, len(audio), spf):
            chunk = audio[i : i + spf]
            if len(chunk) < spf:
                chunk = np.pad(chunk, (0, spf - len(chunk)))
            stream.push_frame(rtc.AudioFrame(chunk.tobytes(), 16000, 1, spf))
            # Real-time pacing.
            target = start + (i + spf) / 16000
            await asyncio.sleep(max(0.0, target - time.monotonic()))
        stream.end_input()
        await asyncio.wait_for(rtask, timeout=WAIT_AFTER_S)
    except asyncio.TimeoutError:
        rtask.cancel()
    except Exception as e:  # noqa: BLE001 - record provider errors per clip
        err = f"{type(e).__name__}: {e}"[:300]
        rtask.cancel()
    finally:
        await stream.aclose()
    latency = (last_final - speech_end) if (last_final and speech_end) else None
    return {
        "id": row["id"], "language": row["language"], "condition": row["condition"], "model": model,
        "reference": row["text"], "hypothesis": " ".join(finals), "latency_s": latency,
        "audio_s": len(pcm) / 16000, "error": err,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--manifest", default="/tmp/asr-bench/manifest.csv")
    ap.add_argument("--out", default="/tmp/asr-bench/results")
    ap.add_argument("--langs", default=",".join(LANG))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=12)
    args = ap.parse_args()
    base = Path(args.manifest).parent
    langs = args.langs.split(",")
    rows = [r for r in csv.DictReader(open(args.manifest, encoding="utf-8")) if r["language"] in langs]
    if args.limit:
        keep: dict = {}
        rows = [r for r in rows if keep.setdefault((r["language"], r["condition"]), []).append(r) or
                len(keep[(r["language"], r["condition"])]) <= args.limit]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / (args.model.replace("/", "__") + ".jsonl")
    done = set()
    if out.exists():
        for line in out.open():
            d = json.loads(line)
            if not d.get("error"):
                done.add((d["id"], d["condition"]))
    todo = [r for r in rows if (r["id"], r["condition"]) not in done]
    print(f"{args.model}: {len(todo)} clips to run ({len(done)} already done)", flush=True)
    sem = asyncio.Semaphore(args.concurrency)
    async with aiohttp.ClientSession() as session:
        async def one(r: dict) -> dict:
            async with sem:
                return await run_clip(args.model, LANG[r["language"]], r, base, session)
        n = 0
        with out.open("a", encoding="utf-8") as f:
            for coro in asyncio.as_completed([one(r) for r in todo]):
                res = await coro
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
                f.flush()
                n += 1
                if n % 100 == 0 or res["error"]:
                    print(n, res["language"], res["condition"], res["error"] or "", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
