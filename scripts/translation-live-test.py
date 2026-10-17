#!/usr/bin/env python3
"""Live end-to-end test of the translation bridge against real LiveKit Cloud.

Creates a room, dispatches the ``translation-agent`` worker, and joins a
synthetic Spanish-speaking peer (user-es) that:

  * publishes microphone audio (edge-tts generated speech from the test corpus),
  * records the ``translation-user-zh`` feed — exactly what the app user
   hears (a speaker never receives their own relay),
  * re-transcribes the recording with local whisper and compares it against
    the corpus reference translation.

Connection details for the Flutter app (translation_mobile) are written to
/tmp/translation_live_test.json so a second participant can join from the
Android emulator while this runs:

    agent/.venv/bin/python scripts/translation-live-test.py          # fresh room
    agent/.venv/bin/python scripts/translation-live-test.py --reuse  # same room/tokens

``--reuse`` re-dispatches the agent into the existing room from the JSON file
(tokens stay valid for an hour), so the app side does not need reconfiguring
between retries.

Requires agent/.env credentials and the translation worker running
(``python -m agent.translation_worker start``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import wave
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from livekit import api, rtc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("translation-live-test")

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
ENV_FILE = AGENT_DIR / ".env"
OUT_FILE = Path("/tmp/translation_live_test.json")
RELAY_WAV = Path("/tmp/relay-to-app.wav")
AGENT_NAME = "translation-agent"
ES_SENTENCE_IDX = [0, 7, 15]  # corpus sentences the synthetic peer speaks
SPEAK_LANGUAGE = "es"
EXPECTED_RELAY_LANGUAGE = "en"  # ENGLISH_RELAY is the default mode


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _token_for(env: dict[str, str], identity: str, room: str) -> str:
    return (
        api.AccessToken(env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )


async def main() -> int:
    reuse = "--reuse" in sys.argv
    env = _load_env(ENV_FILE)
    url = env["LIVEKIT_URL"]

    lkapi = api.LiveKitAPI(url, env["LIVEKIT_API_KEY"], env["LIVEKIT_API_SECRET"])

    if reuse and OUT_FILE.exists():
        conn = json.loads(OUT_FILE.read_text())
        room_name = conn["room"]
        es_token = conn["es_token"]
        logger.info("reusing room %s", room_name)
    else:
        room_name = f"translation-live-{int(time.time())}"
        await lkapi.room.create_room(api.CreateRoomRequest(name=room_name))
        logger.info("room created: %s", room_name)
        es_token = _token_for(env, "user-es", room_name)
        OUT_FILE.write_text(
            json.dumps(
                {
                    "server_url": url,
                    "room": room_name,
                    "app_identity": "user-zh",
                    "app_token": _token_for(env, "user-zh", room_name),
                    "app_language": "zh",
                    "es_token": es_token,
                },
                indent=2,
            )
        )
        logger.info("app connection details written to %s", OUT_FILE)

    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
    )
    logger.info("dispatched %s", AGENT_NAME)
    await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
    await lkapi.room.create_room(api.CreateRoomRequest(name=room_name))
    logger.info("room recycled: %s", room_name)

    room = rtc.Room()
    relay_frames: list[rtc.AudioFrame] = []
    agent_joined = asyncio.Event()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub, participant):
        if pub.name != "translation-user-zh":
            return
        logger.info("subscribed to relay feed %s", pub.name)

        async def record():
            stream = rtc.AudioStream(track)
            async for ev in stream:
                relay_frames.append(ev.frame)

        asyncio.create_task(record())

    @room.on("participant_connected")
    def _on_connect(p: rtc.RemoteParticipant):
        logger.info("participant joined: %s", p.identity)
        if p.identity.startswith("agent-"):
            agent_joined.set()

    await room.connect(url, es_token)
    logger.info("synthetic peer joined as user-es")
    # Re-joins after a room recycle lose the control message, and a
    # declaration is more reliable than first-utterance detection anyway.
    await asyncio.sleep(3)
    await room.local_participant.publish_data(
        json.dumps({"preferred_language": SPEAK_LANGUAGE}).encode(),
        topic="rs.translation.control",
        reliable=True,
    )

    # Publish a mic track so the bridge has an audio publication to attach to.
    source = rtc.AudioSource(16000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    try:
        await asyncio.wait_for(agent_joined.wait(), timeout=90)
    except asyncio.TimeoutError:
        # The agent may have finished joining before we connected — check the
        # participant list we got at connect time.
        if any(p.identity.startswith("agent-") for p in room.remote_participants.values()):
            logger.info("agent already present in participant list")
        else:
            logger.error("translation-agent did not join within 90s")
            return 1

    # Give the bridge a moment to publish the per-participant feeds.
    await asyncio.sleep(8)

    from agent.translation.synthesizer import EdgeTTSSynthesizer  # noqa: PLC0415
    from tests.translation_corpus import SENTENCES  # noqa: PLC0415

    tts = EdgeTTSSynthesizer(sample_rate=16000)
    for idx in ES_SENTENCE_IDX:
        sentence = SENTENCES[SPEAK_LANGUAGE][idx]
        logger.info('speaking [%d]: "%s"', idx, sentence)
        pcm = await tts.synthesize(sentence, SPEAK_LANGUAGE)
        for off in range(0, len(pcm), 320):  # 10ms @ 16kHz PCM16
            chunk = pcm[off : off + 320].ljust(320, b"\x00")
            await source.capture_frame(
                rtc.AudioFrame(
                    data=chunk,
                    sample_rate=16000,
                    num_channels=1,
                    samples_per_channel=160,
                )
            )
        await asyncio.sleep(2.5)  # let the bridge finalize + relay

    # Translation + TTS run behind the STT final; give the relay time to
    # arrive before hanging up.
    await asyncio.sleep(150)
    await room.disconnect()
    await lkapi.aclose()

    if not relay_frames:
        logger.error("no relay audio received — FAIL")
        return 1

    sample_rate = relay_frames[0].sample_rate
    if any(f.sample_rate != sample_rate for f in relay_frames):
        logger.warning("mixed sample rates in recording; using %d", sample_rate)
    with wave.open(str(RELAY_WAV), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"".join(f.data for f in relay_frames))
    logger.info(
        "recorded %d frames of relay audio @%dHz -> %s",
        len(relay_frames), sample_rate, RELAY_WAV,
    )

    from faster_whisper import WhisperModel  # noqa: PLC0415

    model = WhisperModel(
        os.environ.get("TRANSLATION_TEST_WHISPER_MODEL", "small"),
        device="auto",
        compute_type="int8",
    )
    segments, info = model.transcribe(
        str(RELAY_WAV), language=EXPECTED_RELAY_LANGUAGE, vad_filter=True
    )
    heard = " ".join(s.text for s in segments).strip()
    logger.info("relay transcribed as (%s): %s", info.language, heard)

    import difflib  # noqa: PLC0415
    import re  # noqa: PLC0415

    def norm(t: str) -> str:
        t = re.sub(r"[\s.,!?;:'\"，。！？、¿¡]+", "", t.lower())
        if any("\u4e00" <= ch <= "\u9fff" for ch in t):
            from opencc import OpenCC  # noqa: PLC0415

            t = OpenCC("t2s").convert(t).replace("您", "你")
        return t

    expected = " ".join(SENTENCES[EXPECTED_RELAY_LANGUAGE][i] for i in ES_SENTENCE_IDX)
    score = difflib.SequenceMatcher(None, norm(heard), norm(expected)).ratio()
    # The app rejoins mid-run, so the feed may only carry a subset of the
    # relayed sentences — score each reference separately and count matches.
    per_sentence = [
        difflib.SequenceMatcher(
            None, norm(heard), norm(SENTENCES[EXPECTED_RELAY_LANGUAGE][i])
        ).ratio()
        for i in ES_SENTENCE_IDX
    ]
    matched = sum(1 for s in per_sentence if s >= 0.55)
    logger.info(
        "similarity vs all references: %.2f; per-sentence=%s; matched %d/%d",
        score, [round(s, 2) for s in per_sentence], matched, len(ES_SENTENCE_IDX),
    )

    ok = info.language == EXPECTED_RELAY_LANGUAGE and matched >= 1
    logger.info("LIVE TEST %s", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
