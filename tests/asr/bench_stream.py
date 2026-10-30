#!/usr/bin/env python3
"""Streaming mode for local models (compare with batch mode in bench_gpu.py).

Audio is fed in fixed chunks as it would arrive live; each decode step is timed
on the GPU and placed on a simulated clock: chunk k is available at
(k+1) * chunk seconds, a step starts when its chunk has arrived and the
previous step is done, and ends after its measured compute time. The stream is
closed VAD_WAIT_S after the end of speech (same end-of-speech detection as the
batch numbers), then the model finalises.

Per clip:
  first_partial_s  speech start -> first non-empty partial text
  latency_s        speech end -> final text (comparable to batch latency)
  hypothesis       final text (scored like batch)

Models (one per run, each in its family's venv):
  qwen3-asr-1.7b-stream     Qwen3-ASR official streaming API (vLLM backend)
  whisper-large-v3-stream   faster-whisper re-decoding the growing buffer
  whisper-large-v3-turbo-stream
  paraformer-zh-stream      FunASR paraformer-zh-streaming (native, 600 ms chunks)
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

SR = 16000
VAD_WAIT_S = 0.5


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def gpu_busy() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


# --- streaming adapters --------------------------------------------------------
# Each returns (languages, chunk_s, new_stream(lang) -> (step(chunk) -> partial text,
#                                                        finish(tail) -> final text)).


def whisper_stream(size: str):
    from faster_whisper import WhisperModel

    m = WhisperModel(size, device="cuda", compute_type="float16")
    code = {"en": "en", "es": "es", "tl": "tl", "tl-en": "tl", "zh": "zh", "zh-tw": "zh"}

    def decode(buf, lang):
        segs, _ = m.transcribe(buf, language=code[lang], beam_size=1, condition_on_previous_text=False,
                               vad_filter=False)
        return "".join(s.text for s in segs).strip()

    def new_stream(lang):
        buf = np.zeros(0, np.float32)

        def step(chunk):
            nonlocal buf
            buf = np.concatenate([buf, chunk])
            return decode(buf, lang)

        def finish(tail):
            nonlocal buf
            buf = np.concatenate([buf, tail])
            return decode(buf, lang)

        return step, finish

    return list(code), 1.0, new_stream


def qwen_stream(repo: str):
    from qwen_asr import Qwen3ASRModel

    m = Qwen3ASRModel.LLM(model=repo, gpu_memory_utilization=0.55, max_model_len=4096, max_new_tokens=128)
    code = {"en": "English", "es": "Spanish", "tl": "Filipino", "tl-en": None, "zh": "Chinese", "zh-tw": "Chinese"}
    chunk_s = 1.0

    def new_stream(lang):
        st = m.init_streaming_state(language=code[lang], chunk_size_sec=chunk_s)

        def step(chunk):
            m.streaming_transcribe(chunk, st)
            return st.text.strip()

        def finish(tail):
            if len(tail):
                m.streaming_transcribe(tail, st)
            m.finish_streaming_transcribe(st)
            return st.text.strip()

        return step, finish

    return list(code), chunk_s, new_stream


def paraformer_stream():
    from funasr import AutoModel

    m = AutoModel(model="paraformer-zh-streaming", device="cuda:0", disable_update=True)
    chunk_size = [0, 10, 5]  # 600 ms chunks, 300 ms lookahead
    chunk_s = 0.6

    def new_stream(lang):
        cache: dict = {}
        text: list[str] = []

        def run(chunk, final):
            res = m.generate(input=chunk, cache=cache, is_final=final, chunk_size=chunk_size,
                             encoder_chunk_look_back=4, decoder_chunk_look_back=1)
            if res and res[0].get("text"):
                text.append(res[0]["text"])
            return "".join(text).replace(" ", "")

        return (lambda chunk: run(chunk, False)), (lambda tail: run(tail if len(tail) else np.zeros(160, np.float32), True))

    return ["zh", "zh-tw"], chunk_s, new_stream


MODELS = {
    "qwen3-asr-1.7b-stream": lambda: qwen_stream("Qwen/Qwen3-ASR-1.7B"),
    "whisper-large-v3-stream": lambda: whisper_stream("Systran/faster-whisper-large-v3"),
    "whisper-large-v3-turbo-stream": lambda: whisper_stream("mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    "paraformer-zh-stream": paraformer_stream,
}


def run_clip(audio: np.ndarray, row: dict, chunk_s: float, new_stream) -> dict:
    step, finish = new_stream(row["language"])
    speech_start = float(row["speech_start_s"])
    speech_end = float(row["speech_end_s"])
    # Close VAD_WAIT_S after speech ends; clips cut mid-sentence get the
    # silence a live caller would produce.
    close_at = speech_end + VAD_WAIT_S
    n_close = int(close_at * SR)
    if len(audio) < n_close:
        audio = np.concatenate([audio, np.zeros(n_close - len(audio), np.float32)])
    cs = int(chunk_s * SR)
    clock = 0.0
    first_partial = None
    k = 0
    while (k + 1) * cs <= n_close:
        chunk = audio[k * cs : (k + 1) * cs]
        arrive = (k + 1) * chunk_s
        t0 = time.perf_counter()
        partial = step(chunk)
        clock = max(clock, arrive) + (time.perf_counter() - t0)
        if partial and first_partial is None:
            first_partial = max(0.0, clock - speech_start)
        k += 1
    tail = audio[k * cs : n_close]
    t0 = time.perf_counter()
    final = finish(tail)
    clock = max(clock, close_at) + (time.perf_counter() - t0)
    if first_partial is None and final:
        first_partial = max(0.0, clock - speech_start)
    return {"hypothesis": final, "latency_s": clock - speech_end, "first_partial_s": first_partial}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(MODELS))
    ap.add_argument("--manifest", default=str(Path.home() / "asr-bench/manifest.csv"))
    ap.add_argument("--out", default=str(Path.home() / "asr-bench/results"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    base = Path(args.manifest).parent
    rows = list(csv.DictReader(open(args.manifest, encoding="utf-8")))
    langs, chunk_s, new_stream = MODELS[args.model]()
    rows = [r for r in rows if r["language"] in langs]
    if args.limit:
        seen: dict = {}
        rows = [r for r in rows if seen.setdefault((r["language"], r["condition"]), []).append(r)
                or len(seen[(r["language"], r["condition"])]) <= args.limit]
    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / f"{args.model}.jsonl"
    done = set()
    if out.exists():
        for line in out.open(encoding="utf-8"):
            d = json.loads(line)
            if not d.get("error"):
                done.add((d["id"], d["condition"]))
    rows = [r for r in rows if (r["id"], r["condition"]) not in done]
    print(f"{args.model}: {len(rows)} clips to run ({len(done)} already done)", flush=True)
    if rows:  # warm-up, untimed
        run_clip(load_wav(str(base / rows[0]["wav"])), rows[0], chunk_s, new_stream)
    with out.open("a", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            audio = load_wav(str(base / r["wav"]))
            busy = gpu_busy() if i % 10 == 0 else None
            res, err = {"hypothesis": "", "latency_s": None, "first_partial_s": None}, None
            try:
                res = run_clip(audio, r, chunk_s, new_stream)
            except Exception as e:  # noqa: BLE001 - record per clip
                err = f"{type(e).__name__}: {e}"[:300]
            f.write(json.dumps({
                "id": r["id"], "language": r["language"], "condition": r["condition"], "model": args.model,
                "reference": r["text"], **res, "audio_s": float(r["audio_s"]), "gpu_busy": busy,
                "error": err, "streaming_local": True, "chunk_s": chunk_s,
            }, ensure_ascii=False) + "\n")
            if (i + 1) % 100 == 0:
                print(f"{i + 1}/{len(rows)}", flush=True)
    print(f"done: {out}", flush=True)


if __name__ == "__main__":
    main()
