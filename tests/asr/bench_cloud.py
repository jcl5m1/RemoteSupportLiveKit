#!/usr/bin/env python3
"""Cloud streaming STT through LiveKit Inference (same path as the agent).

Each clip is streamed in real time (20 ms frames) followed by 2 s of silence;
latency = time from the end of the speech (manifest speech_end_s) to the last
final transcript, i.e.
what a caller waits after they stop talking. Run with the agent venv and its
LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET:

  agent/.venv/bin/python tests/asr/bench_cloud.py --model deepgram/nova-3 \
      --manifest /tmp/asr-bench/manifest.csv --out /tmp/asr-bench/results

QUOTA: this spends LiveKit Cloud inference credit, which on the free Build plan
is a small hard monthly cap shared with the production agent (exhausting it
silences the agent until the 1st). So the script asks first (quota_guard.py):
no "yes" within 30 s -> nothing is sent. Defaults are deliberately small
(5 clips per language and condition, at most 10 audio-minutes per run, 3
parallel streams), and the run stops at the first sign of rate limiting.
"""

import argparse
import asyncio
import csv
import json
import time
import wave
from pathlib import Path

import sys

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
    ap.add_argument("--model", required=True, help="LiveKit Inference model(s), comma-separated")
    ap.add_argument("--manifest", default="/tmp/asr-bench/manifest.csv")
    ap.add_argument("--out", default="/tmp/asr-bench/results")
    ap.add_argument("--langs", default=",".join(LANG))
    ap.add_argument("--limit", type=int, default=5, help="clips per language and condition (0 = all)")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel streams (Build plan allows 5)")
    ap.add_argument("--max-minutes", type=float, default=10.0,
                    help="refuse to run if the planned audio exceeds this many minutes")
    ap.add_argument("--approved-livekit-usage", action="store_true",
                    help="skip the prompt -- only after the project owner approved this run")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from quota_guard import require_approval

    models = [m.strip() for m in args.model.split(",") if m.strip()]
    plan = [(m, _todo(args, m)) for m in models]
    minutes = sum(float(r["audio_s"]) + TAIL_S for _, todo in plan for r in todo) / 60
    for m, todo in plan:
        print(f"{m}: {len(todo)} clips to run", flush=True)
    if minutes == 0:
        return
    if minutes > args.max_minutes:
        print(f"Planned {minutes:.1f} audio-minutes exceeds --max-minutes {args.max_minutes:g}; "
              "lower --limit or raise --max-minutes deliberately.")
        return
    if not require_approval(f"bench_cloud.py ({', '.join(models)})", minutes, args.approved_livekit_usage):
        return
    for m, todo in plan:
        if not await _run_model(args, m, todo):
            print("Stopped: LiveKit is refusing requests (quota or rate limit) -- not retrying.")
            return


def _todo(args, model: str) -> list[dict]:
    langs = args.langs.split(",")
    rows = [r for r in csv.DictReader(open(args.manifest, encoding="utf-8")) if r["language"] in langs]
    if args.limit:
        keep: dict = {}
        rows = [r for r in rows if keep.setdefault((r["language"], r["condition"]), []).append(r) or
                len(keep[(r["language"], r["condition"])]) <= args.limit]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / (model.replace("/", "__") + ".jsonl")
    done = set()
    if out.exists():
        for line in out.open():
            d = json.loads(line)
            if not d.get("error"):
                done.add((d["id"], d["condition"]))
    return [r for r in rows if (r["id"], r["condition"]) not in done]


async def _run_model(args, model: str, todo: list[dict]) -> bool:
    """Run one model's clips; False if LiveKit started refusing requests."""
    base = Path(args.manifest).parent
    out = Path(args.out) / (model.replace("/", "__") + ".jsonl")
    sem = asyncio.Semaphore(args.concurrency)
    refused = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        async def one(r: dict) -> dict | None:
            async with sem:
                if refused.is_set():
                    return None  # stop sending once LiveKit refuses
                return await run_clip(model, LANG[r["language"]], r, base, session)

        n = failures = 0
        with out.open("a", encoding="utf-8") as f:
            for coro in asyncio.as_completed([one(r) for r in todo]):
                res = await coro
                if res is None:
                    continue
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
                f.flush()
                n += 1
                # "SpeechStream is closed" is how a 429 surfaces through the SDK.
                if res["error"] and ("429" in res["error"] or "closed" in res["error"]):
                    failures += 1
                    if failures >= 2:
                        refused.set()
                if n % 100 == 0 or res["error"]:
                    print(n, res["language"], res["condition"], res["error"] or "", flush=True)
    return not refused.is_set()


if __name__ == "__main__":
    asyncio.run(main())
