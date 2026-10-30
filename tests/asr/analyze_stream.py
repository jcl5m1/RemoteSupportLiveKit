#!/usr/bin/env python3
"""Batch vs streaming for the local models (bench_gpu.py vs bench_stream.py).

One figure per condition, one panel per language: error vs seconds from end of
speech to final text. Hollow circle = batch, filled = streaming, arrow from
batch to streaming of the same model; streaming points are labelled with the
median time from speech start to the first partial words.

  .venv/bin/python analyze_stream.py --results /tmp/asr-bench/results
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from analyze_v2 import CJK, CONDITIONS, LANGS, VAD_WAIT_S, score

PAIRS = [  # (label, batch model, streaming model, colour) -- fixed order/colours
    ("Qwen3-ASR 1.7B", "qwen3-asr-1.7b", "qwen3-asr-1.7b-stream", "#eb6834"),
    ("Whisper v3", "whisper-large-v3", "whisper-large-v3-stream", "#2a78d6"),
    ("Whisper v3 Turbo", "whisper-large-v3-turbo", "whisper-large-v3-turbo-stream", "#1baf7a"),
    ("Paraformer (zh)", "paraformer-zh", "paraformer-zh-stream", "#eda100"),
]


def load(results: Path) -> dict:
    groups: dict = defaultdict(list)
    for path in results.glob("*.jsonl"):
        for line in path.open(encoding="utf-8"):
            d = json.loads(line)
            if not d.get("error"):
                groups[(d["model"], d["language"], d["condition"])].append(d)
    return groups


def summarise(rows: list[dict], lang: str, streaming: bool) -> dict:
    lat = [r["latency_s"] for r in rows if r.get("latency_s") is not None]
    if not streaming:
        lat = [x + VAD_WAIT_S for x in lat]  # batch: compute after end-of-speech detection
    first = [r["first_partial_s"] for r in rows if r.get("first_partial_s") is not None]
    return {
        "error": score(rows, lang),
        "latency_p50_s": float(np.median(lat)),
        "first_words_p50_s": float(np.median(first)) if first else None,
        "n": len(rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="/tmp/asr-bench/results")
    args = ap.parse_args()
    groups = load(Path(args.results))

    table = []
    for label, bm, sm, _ in PAIRS:
        for lang, _ in LANGS:
            for cond, _ in CONDITIONS:
                for mode, model in (("batch", bm), ("streaming", sm)):
                    rows = groups.get((model, lang, cond))
                    if rows and len(rows) >= 50:
                        table.append({"model": label, "mode": mode, "language": lang, "condition": cond,
                                      **summarise(rows, lang, mode == "streaming")})
    with open("results_stream.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(table[0]))
        w.writeheader()
        w.writerows(table)
    print(f"wrote results_stream.csv: {len(table)} rows")
    for cond, title in CONDITIONS:
        plot([r for r in table if r["condition"] == cond], cond, title)


def plot(table: list[dict], cond: str, title: str) -> None:
    from adjustText import adjust_text

    SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    colour = {p[0]: p[3] for p in PAIRS}
    fig, axes = plt.subplots(2, 3, figsize=(17, 10.5), facecolor=SURFACE)
    for ax, (lang, lang_title) in zip(axes.flat, LANGS):
        ax.set_facecolor(SURFACE)
        metric = "CER" if lang in CJK else "WER"
        rows = [r for r in table if r["language"] == lang]
        if not rows:
            ax.set_title(lang_title, loc="left", color=INK, fontsize=12)
            ax.text(0.5, 0.5, "no data for this condition", transform=ax.transAxes, ha="center", color=INK2)
            ax.set_axis_off()
            continue
        texts = []
        top = max(5.0, max(r["error"] * 100 for r in rows) * 1.25)
        for label, *_ in PAIRS:
            b = next((r for r in rows if r["model"] == label and r["mode"] == "batch"), None)
            s = next((r for r in rows if r["model"] == label and r["mode"] == "streaming"), None)
            c = colour[label]
            if b:
                ax.scatter(b["latency_p50_s"], b["error"] * 100, s=80, facecolor=SURFACE, edgecolor=c,
                           linewidth=2, zorder=3)
            if s:
                ax.scatter(s["latency_p50_s"], s["error"] * 100, s=80, facecolor=c, edgecolor=SURFACE,
                           linewidth=1.5, zorder=4)
                fw = f", first words {s['first_words_p50_s']:.1f}s" if s["first_words_p50_s"] is not None else ""
                texts.append(ax.text(s["latency_p50_s"], s["error"] * 100, f"{label}{fw}", fontsize=8,
                                     color=INK2, zorder=5))
            if b and s:
                ax.annotate("", xy=(s["latency_p50_s"], s["error"] * 100),
                            xytext=(b["latency_p50_s"], b["error"] * 100),
                            arrowprops=dict(arrowstyle="->", color=c, lw=1.2, alpha=0.7), zorder=2)
        ax.set_xlim(0.4, 2.6)
        ax.set_ylim(0, top)
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(INK2)
        ax.tick_params(colors=INK2, labelsize=9)
        ax.set_title(f"{lang_title}  ·  {metric}", loc="left", color=INK, fontsize=12)
        ax.set_xlabel("seconds from end of speech to final text (median)", color=INK2, fontsize=9)
        ax.set_ylabel(f"{metric}, lower is better", color=INK2, fontsize=9)
        if texts:
            adjust_text(texts, ax=ax, arrowprops=dict(arrowstyle="-", color=GRID, lw=0.6))
    handles = [plt.Line2D([], [], marker="o", ls="", markersize=9, markerfacecolor=c, markeredgecolor=c, label=l)
               for l, *_, c in PAIRS]
    handles += [plt.Line2D([], [], marker="o", ls="", markersize=9, markerfacecolor=SURFACE,
                           markeredgecolor="#888", markeredgewidth=2, label="batch (hollow)"),
                plt.Line2D([], [], marker="o", ls="", markersize=9, markerfacecolor="#888",
                           markeredgecolor="#888", label="streaming (filled)")]
    fig.suptitle(f"Batch vs streaming, local models on RTX 3090  ·  {title}", x=0.01, ha="left",
                 color=INK, fontsize=14, y=0.995)
    fig.legend(handles=handles, loc="upper center", ncol=6, frameon=False, fontsize=10, labelcolor=INK,
               bbox_to_anchor=(0.5, 0.965))
    fig.text(0.01, 0.005,
             f"Arrow: batch -> streaming for the same model. Both include a {VAD_WAIT_S}s end-of-speech wait. "
             "Batch transcribes the whole utterance after it ends; streaming decodes 1 s chunks as audio arrives "
             "(Paraformer: 0.6 s), so text appears while the person is still talking.\n"
             "'first words' = median seconds from the start of speech to the first partial text. "
             "Down-left is better. Mandarin scored after Traditional->Simplified conversion.",
             fontsize=8.5, color=INK2, va="bottom")
    fig.tight_layout(rect=(0, 0.04, 1, 0.93))
    out = f"asr_batch_vs_stream_{cond}.png"
    fig.savefig(out, dpi=140, facecolor=SURFACE)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
