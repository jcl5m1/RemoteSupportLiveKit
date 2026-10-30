#!/usr/bin/env python3
"""Add Qwen3-ASR-1.7B to the benchmark: transcribe every clip in dataset.csv
and merge the rows into transcriptions.csv (replacing earlier Qwen rows).
Then re-score with eval.py, which reuses the saved transcriptions.

Separate venv (qwen-asr pins transformers 4.57.6):
  python3 -m venv .venv-qwen
  .venv-qwen/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
  .venv-qwen/bin/pip install qwen-asr
  .venv-qwen/bin/python eval_qwen.py [--device cpu|cuda:0] [--limit N]
"""

import argparse
import csv
import time

import torch
from qwen_asr import Qwen3ASRModel

MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
MODEL_NAME = "Qwen3-ASR-1.7B"
# Forced language, like the Whisper runs (zh-tw is Mandarin too).
QWEN_LANG = {"en": "English", "zh": "Chinese", "zh-tw": "Chinese", "es": "Spanish", "tl": "Filipino"}
FIELDS = ["id", "language", "model", "reference", "hypothesis"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--limit", type=int, default=0, help="first N clips per language (timing runs)")
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()

    rows = list(csv.DictReader(open("dataset.csv", encoding="utf-8")))
    if args.limit:
        seen: dict[str, int] = {}
        rows = [r for r in rows if (seen.__setitem__(r["language"], seen.get(r["language"], 0) + 1)
                                    or seen[r["language"]] <= args.limit)]

    model = Qwen3ASRModel.from_pretrained(
        MODEL_ID,
        dtype=torch.bfloat16,
        device_map=args.device,
        max_inference_batch_size=args.batch,
        max_new_tokens=128,
    )
    out = []
    start = time.time()
    for lang in QWEN_LANG:
        group = [r for r in rows if r["language"] == lang]
        for i in range(0, len(group), args.batch):
            chunk = group[i : i + args.batch]
            results = model.transcribe(
                audio=[r["audio_path"] for r in chunk], language=[QWEN_LANG[lang]] * len(chunk)
            )
            for r, res in zip(chunk, results):
                out.append({"id": r["id"], "language": lang, "model": MODEL_NAME,
                            "reference": r["text"], "hypothesis": res.text.strip()})
        print(f"{lang}: {len(group)} clips, {time.time() - start:.0f}s elapsed", flush=True)

    if args.limit:
        for o in out[:: max(1, args.limit)]:
            print(o["language"], "|", o["reference"], "->", o["hypothesis"])
        print(f"{(time.time() - start) / max(1, len(out)):.2f}s per clip")
        return
    existing = [t for t in csv.DictReader(open("transcriptions.csv", encoding="utf-8")) if t["model"] != MODEL_NAME]
    with open("transcriptions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing + out)
    print(f"merged {len(out)} {MODEL_NAME} rows into transcriptions.csv")


if __name__ == "__main__":
    main()
