#!/usr/bin/env python3
"""Local (GPU) ASR models for the hardened benchmark.

Each model family has its own venv (their dependencies conflict); run one
model per invocation:

  python bench_gpu.py --model whisper-large-v3-turbo --manifest ~/asr-bench/manifest.csv

Latency = wall time to transcribe one clip (batch size 1, audio already in
memory, after warm-up) -- the compute a caller waits for once their speech is
complete. GPU utilisation from other processes is sampled and stored with each
row (``gpu_busy``) so contended timings can be spotted.
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


def load_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def gpu_busy() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        return int(out.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


# --- adapters ----------------------------------------------------------------
# Each returns (supported_languages, transcribe(audio, lang) -> str).


def faster_whisper(name: str, size: str):
    from faster_whisper import WhisperModel

    m = WhisperModel(size, device="cuda", compute_type="float16")
    code = {"en": "en", "es": "es", "tl": "tl", "tl-en": "tl", "zh": "zh", "zh-tw": "zh"}

    def run(audio, lang):
        segs, _ = m.transcribe(audio, language=code[lang], beam_size=1, condition_on_previous_text=False)
        return "".join(s.text for s in segs).strip()

    return list(code), run


def qwen(name: str, repo: str):
    import torch
    from qwen_asr import Qwen3ASRModel

    m = Qwen3ASRModel.from_pretrained(repo, dtype=torch.bfloat16, device_map="cuda:0",
                                      max_inference_batch_size=1, max_new_tokens=128)
    # Taglish: let it detect (it mixes Filipino and English).
    code = {"en": "English", "es": "Spanish", "tl": "Filipino", "tl-en": None, "zh": "Chinese", "zh-tw": "Chinese"}

    def run(audio, lang):
        return m.transcribe(audio=(audio, SR), language=code[lang])[0].text.strip()

    return list(code), run


def funasr_model(name: str, repo: str):
    from funasr import AutoModel

    sensevoice = "SenseVoice" in repo
    kw = dict(model=repo, device="cuda:0", disable_update=True)
    if not sensevoice:
        kw.update(vad_model=None, punc_model=None)
    m = AutoModel(**kw)
    langs = ["zh", "zh-tw", "en"] if sensevoice else ["zh", "zh-tw"]

    def run(audio, lang):
        if sensevoice:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            res = m.generate(input=audio, language="en" if lang == "en" else "zh", use_itn=True)
            return rich_transcription_postprocess(res[0]["text"]).strip()
        return m.generate(input=audio)[0]["text"].replace(" ", "").strip()

    return langs, run


def nemo_model(name: str, repo: str):
    import nemo.collections.asr as nemo_asr

    canary = "canary" in repo
    cls = nemo_asr.models.EncDecMultiTaskModel if canary else nemo_asr.models.ASRModel
    # Half precision: the GPU is shared and only a few GB are free.
    m = cls.from_pretrained(repo).half().cuda().eval()

    def run(audio, lang):
        if canary:
            out = m.transcribe([audio], source_lang=lang, target_lang=lang, batch_size=1, verbose=False)
        else:
            out = m.transcribe([audio], batch_size=1, verbose=False)
        h = out[0]
        return (h.text if hasattr(h, "text") else str(h)).strip()

    return ["en", "es"], run


def omnilingual(name: str, card: str):
    from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline

    p = ASRInferencePipeline(model_card=card, device="cuda")
    code = {"en": "eng_Latn", "es": "spa_Latn", "tl": "tgl_Latn", "tl-en": "tgl_Latn",
            "zh": "cmn_Hans", "zh-tw": "cmn_Hant"}

    def run(audio, lang):
        inp = [{"waveform": audio, "sample_rate": SR}]
        kw = {"lang": [code[lang]]} if "LLM" in card else {}
        return p.transcribe(inp, batch_size=1, **kw)[0].strip()

    return list(code), run


MODELS = {
    "whisper-large-v3-turbo": lambda: faster_whisper("whisper-large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    "whisper-large-v3": lambda: faster_whisper("whisper-large-v3", "Systran/faster-whisper-large-v3"),
    "whisper-medium": lambda: faster_whisper("whisper-medium", "medium"),
    "qwen3-asr-1.7b": lambda: qwen("qwen3-asr-1.7b", "Qwen/Qwen3-ASR-1.7B"),
    "qwen3-asr-0.6b": lambda: qwen("qwen3-asr-0.6b", "Qwen/Qwen3-ASR-0.6B"),
    "paraformer-zh": lambda: funasr_model("paraformer-zh", "paraformer-zh"),
    "sensevoice-small": lambda: funasr_model("sensevoice-small", "iic/SenseVoiceSmall"),
    "parakeet-tdt-0.6b-v3": lambda: nemo_model("parakeet-tdt-0.6b-v3", "nvidia/parakeet-tdt-0.6b-v3"),
    "canary-1b-v2": lambda: nemo_model("canary-1b-v2", "nvidia/canary-1b-v2"),
    "omniasr-llm-1b": lambda: omnilingual("omniasr-llm-1b", "omniASR_LLM_1B"),
    "omniasr-ctc-1b": lambda: omnilingual("omniasr-ctc-1b", "omniASR_CTC_1B"),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(MODELS))
    ap.add_argument("--manifest", default=str(Path.home() / "asr-bench/manifest.csv"))
    ap.add_argument("--out", default=str(Path.home() / "asr-bench/results"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    base = Path(args.manifest).parent
    rows = list(csv.DictReader(open(args.manifest, encoding="utf-8")))

    t0 = time.time()
    langs, run = MODELS[args.model]()
    print(f"{args.model}: loaded in {time.time() - t0:.0f}s; languages {langs}", flush=True)
    rows = [r for r in rows if r["language"] in langs]
    if args.limit:
        seen: dict = {}
        rows = [r for r in rows if seen.setdefault((r["language"], r["condition"]), []).append(r)
                or len(seen[(r["language"], r["condition"])]) <= args.limit]


    Path(args.out).mkdir(parents=True, exist_ok=True)
    out = Path(args.out) / f"{args.model}.jsonl"
    # Resume: skip clips already transcribed (new clips can be added later).
    done = set()
    if out.exists():
        for line in out.open(encoding="utf-8"):
            d = json.loads(line)
            if not d.get("error"):
                done.add((d["id"], d["condition"]))
    rows = [r for r in rows if (r["id"], r["condition"]) not in done]
    print(f"{len(rows)} clips to run ({len(done)} already done)", flush=True)
    if not rows:
        return
    # Warm up (CUDA kernels, caches) on a few clips, untimed.
    for r in rows[:3]:
        run(load_wav(str(base / r["wav"])), r["language"])
    with out.open("a", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            audio = load_wav(str(base / r["wav"]))
            busy = gpu_busy() if i % 10 == 0 else None
            err, hyp = None, ""
            start = time.perf_counter()
            try:
                hyp = run(audio, r["language"])
            except Exception as e:  # noqa: BLE001 - record per clip
                err = f"{type(e).__name__}: {e}"[:300]
            latency = time.perf_counter() - start
            f.write(json.dumps({
                "id": r["id"], "language": r["language"], "condition": r["condition"], "model": args.model,
                "reference": r["text"], "hypothesis": hyp, "latency_s": latency,
                "audio_s": float(r["audio_s"]), "gpu_busy": busy, "error": err,
            }, ensure_ascii=False) + "\n")
            if (i + 1) % 100 == 0:
                print(f"{i + 1}/{len(rows)}", flush=True)
    print(f"done: {out}", flush=True)


if __name__ == "__main__":
    main()
