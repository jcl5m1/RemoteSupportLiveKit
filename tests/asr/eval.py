#!/usr/bin/env python3
"""Run ASR inference and compute WER/CER for the synthetic benchmark."""
import csv
import json
import logging
import os
import re
from collections import defaultdict
from itertools import product
from multiprocessing import Pool
from pathlib import Path

import jiwer
import matplotlib.pyplot as plt
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATASET_CSV = "dataset.csv"
TRANSCRIPTIONS_CSV = "transcriptions.csv"
RESULTS_CSV = "results.csv"
PLOT_PNG = "asr_benchmark_wer_cer.png"

LANG_NAMES = {
    "en": "English",
    "zh": "Chinese Mandarin",
    "zh-tw": "Taiwan Mandarin",
    "es": "Spanish",
    "tl": "Tagalog",
}

WHISPER_LANG = {
    "en": "en",
    "zh": "zh",
    "zh-tw": "zh",
    "es": "es",
    "tl": "tl",
}


def clean_text(text):
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def transcribe_whisper(model, path, lang_code):
    segments, _ = model.transcribe(
        path,
        language=lang_code,
        beam_size=1,
        best_of=1,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    return "".join(s.text for s in segments).strip()


def process_language(args):
    lang, rows = args
    os.environ["OMP_NUM_THREADS"] = "4"
    os.environ["MKL_NUM_THREADS"] = "4"
    os.environ["OPENBLAS_NUM_THREADS"] = "4"

    import torch

    torch.set_num_threads(4)

    results = []

    from faster_whisper import WhisperModel

    logger.info(f"[{lang}] loading Whisper large-v3-turbo")
    turbo = WhisperModel("large-v3-turbo", compute_type="int8", cpu_threads=4)
    logger.info(f"[{lang}] loading Whisper medium")
    medium = WhisperModel("medium", compute_type="int8", cpu_threads=4)

    for row in rows:
        path = row["audio_path"]
        ref = row["text"]
        code = WHISPER_LANG[lang]
        results.append(
            {
                "id": row["id"],
                "language": lang,
                "model": "Whisper Large v3 Turbo",
                "reference": ref,
                "hypothesis": transcribe_whisper(turbo, path, code),
            }
        )
        results.append(
            {
                "id": row["id"],
                "language": lang,
                "model": "Whisper Medium",
                "reference": ref,
                "hypothesis": transcribe_whisper(medium, path, code),
            }
        )

    if lang in ("zh", "zh-tw"):
        from funasr import AutoModel

        logger.info(f"[{lang}] loading Paraformer-zh")
        para = AutoModel(
            model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            disable_update=True,
        )
        for row in rows:
            res = para.generate(input=row["audio_path"])
            hyp = res[0]["text"] if res and isinstance(res, list) else ""
            results.append(
                {
                    "id": row["id"],
                    "language": lang,
                    "model": "Paraformer-zh",
                    "reference": row["text"],
                    "hypothesis": hyp,
                }
            )

    if lang == "es":
        from transformers import pipeline

        logger.info(f"[{lang}] loading Wav2Vec2-XLSR-Spanish")
        asr = pipeline(
            "automatic-speech-recognition",
            model="jonatasgrosman/wav2vec2-large-xlsr-53-spanish",
            device=-1,
        )
        for row in rows:
            hyp = asr(row["audio_path"])["text"]
            results.append(
                {
                    "id": row["id"],
                    "language": lang,
                    "model": "Wav2Vec2-XLSR-Spanish",
                    "reference": row["text"],
                    "hypothesis": hyp,
                }
            )

    return results


def compute_metrics(transcriptions):
    groups = defaultdict(list)
    for t in transcriptions:
        groups[(t["language"], t["model"])].append(
            (clean_text(t["reference"]), clean_text(t["hypothesis"]))
        )

    results = []
    for (lang, model), pairs in groups.items():
        refs, hyps = zip(*pairs)
        try:
            wer = jiwer.wer(list(refs), list(hyps))
            cer = jiwer.cer(list(refs), list(hyps))
        except Exception as exc:
            logger.warning(f"metric error for {lang}/{model}: {exc}")
            wer, cer = float("nan"), float("nan")
        results.append(
            {
                "language": lang,
                "model": model,
                "language_name": LANG_NAMES[lang],
                "wer": wer,
                "cer": cer,
                "n": len(pairs),
            }
        )
    return results


def plot_results(df):
    languages = [LANG_NAMES[l] for l in ["en", "zh", "zh-tw", "es", "tl"]]
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    for ax, metric in zip(axes, ["wer", "cer"]):
        pivot = df.pivot(index="language_name", columns="model", values=metric)
        pivot = pivot.reindex(languages)
        pivot.plot(kind="bar", ax=ax, width=0.75)
        ax.set_title(f"{'Word' if metric=='wer' else 'Character'} Error Rate by Language")
        ax.set_xlabel("Language")
        ax.set_ylabel("Error Rate")
        ax.set_ylim(0, max(0.05, df[metric].max() * 1.15))
        ax.legend(title="Model", bbox_to_anchor=(1.02, 1), loc="upper left")
        ax.grid(axis="y", linestyle="--", alpha=0.5)

    plt.tight_layout()
    plt.savefig(PLOT_PNG, dpi=150)
    logger.info(f"Saved plot to {PLOT_PNG}")


def main():
    rows = []
    with open(DATASET_CSV, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)

    by_lang = defaultdict(list)
    for row in rows:
        by_lang[row["language"]].append(row)

    if Path(TRANSCRIPTIONS_CSV).exists():
        logger.info(f"Loading existing {TRANSCRIPTIONS_CSV}")
        with open(TRANSCRIPTIONS_CSV, "r", encoding="utf-8") as f:
            transcriptions = list(csv.DictReader(f))
    else:
        logger.info("Running ASR inference (5 language workers)")
        with Pool(processes=5) as pool:
            all_results = pool.map(process_language, list(by_lang.items()))
        transcriptions = [item for sublist in all_results for item in sublist]

        with open(TRANSCRIPTIONS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["id", "language", "model", "reference", "hypothesis"]
            )
            writer.writeheader()
            writer.writerows(transcriptions)

    metrics = compute_metrics(transcriptions)
    df = pd.DataFrame(metrics)
    df = df.sort_values(["language", "model"])
    df.to_csv(RESULTS_CSV, index=False)
    logger.info(f"Saved results to {RESULTS_CSV}")
    print(df.to_string(index=False))

    plot_results(df)


if __name__ == "__main__":
    main()
