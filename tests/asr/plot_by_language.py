#!/usr/bin/env python3
"""Grouped bar chart of results.csv: languages on x, one colour per model."""
import csv

import matplotlib.pyplot as plt

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
MODELS = [  # fixed order -> fixed colour (validated categorical slots 1-5)
    ("Whisper Large v3 Turbo", "#2a78d6"),
    ("Whisper Medium", "#eb6834"),
    ("Paraformer-zh", "#1baf7a"),
    ("Wav2Vec2-XLSR-Spanish", "#eda100"),
    ("Qwen3-ASR-1.7B", "#e87ba4"),
]
LANGS = ["English", "Spanish", "Tagalog", "Chinese Mandarin", "Taiwan Mandarin"]
LABELS = {"Chinese Mandarin": "Mandarin\n(mainland)", "Taiwan Mandarin": "Mandarin\n(Taiwan)"}

rows = {(r["language_name"], r["model"]): r for r in csv.DictReader(open("results.csv"))}

fig, axes = plt.subplots(2, 1, figsize=(11, 8.2), facecolor=SURFACE)
for ax, metric, title in zip(axes, ["wer", "cer"], ["Word error rate (WER)", "Character error rate (CER)"]):
    ax.set_facecolor(SURFACE)
    width, gap = 0.16, 0.02
    top = 110 if metric == "wer" else 45
    stub = top * 0.012  # zero-error bars stay visible (colour = model)
    for li, lang in enumerate(LANGS):
        present = [(m, c) for m, c in MODELS if (lang, m) in rows]
        start = li - (len(present) * width) / 2 + width / 2
        for k, (m, c) in enumerate(present):
            v = float(rows[(lang, m)][metric]) * 100
            x = start + k * width
            ax.bar(x, max(v, stub), width - gap, color=c, edgecolor=SURFACE, linewidth=0)
            ax.text(x, max(v, stub) + top * 0.012, f"{v:.1f}%" if v < 10 else f"{v:.0f}%",
                    ha="center", va="bottom", fontsize=8.5, color=INK2)
    ax.set_xticks(range(len(LANGS)), [LABELS.get(l, l) for l in LANGS], color=INK, fontsize=10.5)
    ax.set_ylim(0, top)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.tick_params(axis="y", colors=INK2, labelsize=9, length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(INK2)
    ax.set_title(title + "  ·  lower is better", loc="left", color=INK, fontsize=12, pad=8)

handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, c in MODELS]
fig.legend(handles, [m for m, _ in MODELS], loc="upper center", ncol=5, frameon=False,
           fontsize=9.5, labelcolor=INK, bbox_to_anchor=(0.5, 0.995))
fig.text(0.01, 0.005,
         "100 synthetic (edge-tts) sentences per language and model; models only shown where evaluated. "
         "Mandarin scores are inflated by Simplified vs Traditional\ncharacter mismatches, and Chinese WER "
         "counts whole sentences (no word spaces) - use CER for Mandarin.",
         fontsize=8.5, color=INK2, va="bottom")
fig.tight_layout(rect=(0, 0.05, 1, 0.95))
fig.savefig("asr_benchmark_by_language.png", dpi=150, facecolor=SURFACE)
print("wrote asr_benchmark_by_language.png")
