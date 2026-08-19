#!/usr/bin/env python3
"""Backend-only load test.

This exercises the HTTP paths that do not require real LiveKit/GCS credentials
(using ALLOW_DEGRADED_START). It is NOT a full LiveKit/media load test; that
requires cloud credentials and a headless LiveKit client.

Usage:
    backend/.venv/bin/python scripts/load-test.py --duration 30 --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402


def _env_default(key: str, default: str) -> str:
    os.environ.setdefault(key, default)
    return os.environ[key]


BASE_URL = _env_default("BACKEND_URL", "http://localhost:8000")
SERVICE_KEY = _env_default("SERVICE_API_KEY", "change-me-too")


async def _measure(
    sem: asyncio.Semaphore,
    fn: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    client: httpx.AsyncClient,
) -> tuple[int, float]:
    async with sem:
        start = time.perf_counter()
        try:
            resp = await fn(client)
            return resp.status_code, time.perf_counter() - start
        except Exception as exc:  # noqa: BLE001
            return -1, time.perf_counter() - start


async def _create_session(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"{BASE_URL}/v1/sessions",
        json={"device_id": f"load-{time.time_ns()}", "display_name": "Load"},
    )


async def _consent(client: httpx.AsyncClient, token: str, session_id: str) -> httpx.Response:
    return await client.post(
        f"{BASE_URL}/v1/sessions/{session_id}/consent",
        json={"accepted": True, "consent_text_version": "v1.0"},
        headers={"Authorization": f"Bearer {token}"},
    )


async def _ingest_utterance(client: httpx.AsyncClient, session_id: str) -> httpx.Response:
    return await client.post(
        f"{BASE_URL}/v1/sessions/{session_id}/utterances",
        json={
            "utterances": [
                {
                    "client_utterance_id": f"u-{time.time_ns()}",
                    "role": "caller",
                    "identity": "caller-1",
                    "source": "agent_stt",
                    "text": "hello",
                    "start_ms": 0,
                    "end_ms": 500,
                    "language": "en",
                }
            ]
        },
        headers={"X-Service-Key": SERVICE_KEY},
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backend-only load test")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent clients")
    args = parser.parse_args()

    sem = asyncio.Semaphore(args.concurrency)
    stop_at = time.perf_counter() + args.duration

    results: dict[str, list[tuple[int, float]]] = {
        "create_session": [],
        "consent": [],
        "ingest_utterance": [],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        tasks: list[asyncio.Task] = []

        def spawn(fn, bucket: str, **kwargs):
            async def wrapper(c: httpx.AsyncClient):
                return await fn(c, **kwargs)

            task = asyncio.create_task(_measure(sem, wrapper, client))
            tasks.append((bucket, task))

        while time.perf_counter() < stop_at:
            spawn(_create_session, "create_session")
            if tasks and len(tasks) % 3 == 0:
                # We need a real session id/token for consent/ingest; do those
                # synchronously every few loops so the test stays simple.
                create_resp = await _create_session(client)
                if create_resp.status_code == 201:
                    data = create_resp.json()
                    spawn(_consent, "consent", token=data["caller_session_token"], session_id=data["session_id"])
                    spawn(_ingest_utterance, "ingest_utterance", session_id=data["session_id"])

            # Drain completed tasks to keep memory bounded.
            done = [(b, t) for b, t in tasks if t.done()]
            for bucket, task in done:
                results[bucket].append(await task)
                tasks.remove((bucket, task))

        # Wait for remaining tasks.
        for bucket, task in tasks:
            results[bucket].append(await task)

    print(f"Duration: {args.duration}s | Concurrency: {args.concurrency}")
    for bucket, entries in results.items():
        if not entries:
            continue
        codes = [s for s, _ in entries]
        latencies = [d for _, d in entries]
        successes = sum(1 for s in codes if 200 <= s < 300)
        failures = len(entries) - successes
        print(
            f"{bucket:20s}: n={len(entries):4d}  ok={successes:4d}  "
            f"fail={failures:4d}  p50={_pct(latencies, 0.5)*1000:.1f}ms  "
            f"p95={_pct(latencies, 0.95)*1000:.1f}ms  p99={_pct(latencies, 0.99)*1000:.1f}ms"
        )


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(q * (len(s) - 1))
    return s[idx]


if __name__ == "__main__":
    asyncio.run(main())
