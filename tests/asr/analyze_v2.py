#!/usr/bin/env python3
"""Score the hardened benchmark and plot error vs latency.

Inputs: results/*.jsonl from bench_cloud.py (streaming services) and
bench_gpu.py (local GPU models). Outputs: results_v2.csv and
asr_error_vs_latency.png.

Scoring
- Mandarin (zh, zh-tw): both sides converted to Simplified (OpenCC t2s), so a
  correct transcript in the other script is not an error; scored by CER.
- Other languages: lower-cased, punctuation and hyphens removed; scored by WER.
- Latency, seconds from the end of speech to the final text:
  streaming services as measured; local models = GPU compute per clip plus a
  fixed VAD_WAIT_S end-of-speech detection (an offline model needs the speech
  to be over before it can start).

  .venv/bin/python analyze_v2.py --results /tmp/asr-bench/results
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import jiwer
import matplotlib.pyplot as plt
import numpy as np
from opencc import OpenCC

VAD_WAIT_S = 0.5
t2s = OpenCC("t2s")

LANGS = [("en", "English"), ("es", "Spanish"), ("tl", "Tagalog"), ("tl-en", "Taglish"),
         ("zh", "Mandarin (mainland)"), ("zh-tw", "Mandarin (Taiwan)")]
CJK = {"zh", "zh-tw"}
CONDITIONS = [
    ("clean", "Synthetic speech, clean"),
    ("phone", "Synthetic speech over a bad phone line (babble noise 10 dB SNR, 8 kHz, Opus 12 kbps)"),
    ("real", "Real recordings (FLEURS read speech; Taiwan: TV drama dialogue)"),
]

# Colour = model family (max 8 categorical slots, fixed order); labels name the model.
FAMILIES = [
    ("Whisper", "#2a78d6"), ("Qwen", "#eb6834"), ("NVIDIA", "#1baf7a"), ("FunASR", "#eda100"),
    ("Meta Omnilingual", "#e87ba4"), ("Deepgram", "#008300"), ("AssemblyAI", "#6250d6"),
    ("Other cloud", "#e34948"),
]
LABELS = {
    "whisper-large-v3-turbo": ("Whisper", "Whisper v3 Turbo"), "whisper-large-v3": ("Whisper", "Whisper v3"),
    "whisper-medium": ("Whisper", "Whisper Medium"), "qwen3-asr-1.7b": ("Qwen", "Qwen3-ASR 1.7B"),
    "qwen3-asr-0.6b": ("Qwen", "Qwen3-ASR 0.6B"), "parakeet-tdt-0.6b-v3": ("NVIDIA", "Parakeet v3"),
    "canary-1b-v2": ("NVIDIA", "Canary 1B v2"), "paraformer-zh": ("FunASR", "Paraformer"),
    "sensevoice-small": ("FunASR", "SenseVoice"), "omniasr-llm-1b": ("Meta Omnilingual", "OmniASR LLM 1B"),
    "omniasr-ctc-1b": ("Meta Omnilingual", "OmniASR CTC 1B"),
    "deepgram/nova-3": ("Deepgram", "Nova-3"), "deepgram/flux-general-multi": ("Deepgram", "Flux multi"),
    "assemblyai/universal-streaming-multilingual": ("AssemblyAI", "AAI Streaming multi*"),
    "assemblyai/universal-3-5-pro": ("AssemblyAI", "AAI Universal-3.5 Pro"),
    "elevenlabs/scribe_v2_realtime": ("Other cloud", "ElevenLabs Scribe v2"),
    "speechmatics/enhanced": ("Other cloud", "Speechmatics"), "cartesia/ink-whisper": ("Other cloud", "Cartesia Ink"),
}
STREAMING = lambda m: "/" in m  # noqa: E731 - cloud models are "provider/model"


def norm_words(s: str) -> str:
    s = s.lower().replace("-", "")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_cjk(s: str) -> str:
    s = t2s.convert(s)
    return re.sub(r"[\W_]+", "", s.lower())


def score(rows: list[dict], lang: str) -> float:
    if lang in CJK:
        refs = [norm_cjk(r["reference"]) for r in rows]
        hyps = [norm_cjk(r["hypothesis"]) for r in rows]
        return jiwer.cer(refs, hyps)
    refs = [norm_words(r["reference"]) for r in rows]
    hyps = [norm_words(r["hypothesis"]) for r in rows]
    return jiwer.wer(refs, hyps)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="/tmp/asr-bench/results")
    ap.add_argument("--out-csv", default="results_v2.csv")
    ap.add_argument("--out-png", default="asr_error_vs_latency.png")
    args = ap.parse_args()

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for path in sorted(Path(args.results).glob("*.jsonl")):
        for line in path.open(encoding="utf-8"):
            d = json.loads(line)
            groups[(d["model"], d["language"], d["condition"])].append(d)

    table = []
    for (model, lang, cond), rows in sorted(groups.items()):
        ok = [r for r in rows if not r.get("error")]
        if len(ok) < 50:
            continue  # provider rejected the language, or too few clips got through
        lat = [r["latency_s"] for r in ok if r.get("latency_s") is not None]
        lat = [x if STREAMING(model) else x + VAD_WAIT_S for x in lat]
        busy = [r["gpu_busy"] for r in ok if r.get("gpu_busy") is not None]
        table.append({
            "model": model, "language": lang, "condition": cond,
            "metric": "CER" if lang in CJK else "WER", "error": round(score(ok, lang), 4),
            "latency_p50_s": round(float(np.median(lat)), 3) if lat else None,
            "latency_p90_s": round(float(np.percentile(lat, 90)), 3) if lat else None,
            "streaming": STREAMING(model), "n": len(ok),
            "gpu_busy_mean": round(float(np.mean(busy)), 1) if busy else None,
        })
    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    print(f"wrote {args.out_csv}: {len(table)} rows")
    stem = args.out_png.removesuffix(".png")
    for cond, title in CONDITIONS:
        plot([r for r in table if r["condition"] == cond], f"{stem}_{cond}.png", title)


def plot(table: list[dict], out: str, title_text: str) -> None:
    from adjustText import adjust_text

    SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    color = dict(FAMILIES)
    fig, axes = plt.subplots(2, 3, figsize=(17, 11), facecolor=SURFACE)
    for ax, (lang, title) in zip(axes.flat, LANGS):
        ax.set_facecolor(SURFACE)
        texts = []
        panel = [r for r in table if r["language"] == lang and r["latency_p50_s"] is not None]
        if not panel:
            ax.set_title(title, loc="left", color=INK, fontsize=12)
            ax.text(0.5, 0.5, "no data for this condition", transform=ax.transAxes, ha="center",
                    color=INK2, fontsize=11)
            ax.set_axis_off()
            continue
        # Cap the axis just above the usable models; worse results sit on the
        # top edge as triangles labelled with their real value.
        usable = [r["error"] * 100 for r in panel if r["error"] <= 0.5]
        top = max(5.0, (max(usable) if usable else 50.0) * 1.2)
        for row in panel:
            fam, label = LABELS.get(row["model"], ("Other cloud", row["model"]))
            c = color[fam]
            x, y = row["latency_p50_s"], row["error"] * 100
            n_note = "" if row["n"] >= 95 else f" (n={row['n']})"
            if y > top:
                ax.scatter(x, top, s=80, marker="^", facecolor=c, edgecolor=SURFACE, linewidth=1.5,
                           zorder=3, clip_on=False)
                texts.append(ax.text(x, top, f"{label} {y:.0f}%{n_note}", fontsize=7.5, color=INK2, zorder=4))
                continue
            ax.scatter(x, y, s=70, marker="o", facecolor=c, edgecolor=SURFACE, linewidth=1.5, zorder=3)
            texts.append(ax.text(x, y, label + n_note, fontsize=7.5, color=INK2, zorder=4))
        ax.set_xscale("log")
        ax.set_xlim(0.08, 6)
        ax.set_xticks([0.1, 0.2, 0.5, 1, 2, 5], ["0.1", "0.2", "0.5", "1", "2", "5"])
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.set_ylim(0, top)
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(INK2)
        ax.tick_params(colors=INK2, labelsize=9)
        metric = "CER" if lang in CJK else "WER"
        ax.set_title(f"{title}  ·  {metric}", loc="left", color=INK, fontsize=12)
        ax.set_xlabel("seconds from end of speech to final text (median, log)", color=INK2, fontsize=9)
        ax.set_ylabel(f"{metric}, lower is better", color=INK2, fontsize=9)
        if texts:
            adjust_text(texts, ax=ax, arrowprops=dict(arrowstyle="-", color=GRID, lw=0.6))
    handles = [plt.Line2D([], [], marker="o", ls="", markersize=8, markerfacecolor=c, markeredgecolor=c, label=f)
               for f, c in FAMILIES]
    fig.suptitle(title_text, x=0.01, ha="left", color=INK, fontsize=14, y=0.995)
    fig.legend(handles=handles, loc="upper center", ncol=8, frameon=False, fontsize=9.5, labelcolor=INK,
               bbox_to_anchor=(0.5, 0.965))
    fig.text(0.01, 0.005,
             "Down-left is better. ~100 clips per language; (n=..) marks fewer clips (cloud runs cut short by a rate limit). "
             "Cloud = LiveKit Inference, "
             f"streamed in real time; local = RTX 3090, compute + {VAD_WAIT_S}s end-of-speech wait.\n"
             "Mandarin scored after Traditional->Simplified conversion. Triangles on the top edge: off the scale (value shown). "
             "Models missing from a panel don't support that language. *the agent's current translation STT.",
             fontsize=8.5, color=INK2, va="bottom")
    fig.tight_layout(rect=(0, 0.03, 1, 0.93))
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
