#!/usr/bin/env python3
"""Synthesize benchmark audio using Microsoft Edge TTS with retries."""
import asyncio
import csv
import json
import os
from pathlib import Path

import edge_tts
from tqdm import tqdm

SEMAPHORE = asyncio.Semaphore(3)
MAX_RETRIES = 3
RETRY_DELAY = 2.0


async def _try_save(text, voice, out_path):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


async def generate_one(lang, voice, item, audio_dir):
    out_path = audio_dir / lang / f"{item['id']}.mp3"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(MAX_RETRIES):
        try:
            async with SEMAPHORE:
                await _try_save(item["text"], voice, out_path)
            return {"id": item["id"], "language": lang, "text": item["text"], "audio_path": str(out_path), "ok": True}
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return {"id": item["id"], "language": lang, "text": item["text"], "audio_path": str(out_path), "ok": False, "error": str(e)}
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))


async def main():
    with open("phrases.json", "r", encoding="utf-8") as f:
        data = json.load(f)

    audio_dir = Path("audio")
    tasks = []
    for lang, cfg in data.items():
        for item in cfg["phrases"]:
            tasks.append(generate_one(lang, cfg["voice"], item, audio_dir))

    rows = []
    for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Synthesizing"):
        rows.append(await coro)

    rows.sort(key=lambda r: r["id"])
    ok_rows = [r for r in rows if r["ok"]]
    fail_rows = [r for r in rows if not r["ok"]]

    with open("dataset.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "language", "text", "audio_path"])
        writer.writeheader()
        writer.writerows([{k: r[k] for k in ["id", "language", "text", "audio_path"]} for r in ok_rows])

    print(f"Success: {len(ok_rows)} / {len(rows)}")
    if fail_rows:
        print(f"Failures: {len(fail_rows)}")
        for r in fail_rows[:10]:
            print(" ", r["id"], r["error"])


if __name__ == "__main__":
    asyncio.run(main())
