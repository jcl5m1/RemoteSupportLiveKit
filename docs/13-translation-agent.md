# 13 — Real-Time Multilingual Translation Bridge

A second LiveKit worker (`agent/agent/translation_worker.py`, agent name
`translation-agent`) that turns an audio-only room into a real-time
interpretation bridge: **N peers, any mix of English, Mandarin, Spanish, and
Tagalog, no camera required**. A Spanish speaker and a Mandarin speaker can
talk past each other; each hears the other in their own language.

Runs as a **separate process** from the support agent — `AgentServer` supports
only one `rtc_session` registration per process (enforced at import time).

```
                 ┌──────────────────────────────────────────────┐
  peer mic (es) ─┤ STT stream (deepgram/nova-3:multi, per peer) │
                 │           ▼ final transcript + language       │
                 │  TranslationCore (pure logic, unit-tested)    │
                 │           ▼                                   │
                 │  translate → one per distinct target language │
                 │           ▼                                   │
                 │  TTS (per-language voices)                    │
                 └───────┬───────────────────────┬──────────────┘
                         ▼                       ▼
        translation-<identity> A        translation-<identity> B
        (private audio feed)            (private audio feed)
                         ▼                       ▼
              peer B hears it in        peer A hears it in
              their own language        their own language
```

## The one rule that shapes everything

> Every participant gets a **private published audio track** named
> `translation-<their identity>`, and **only ever hears that track**.

That single decision gives us, structurally rather than by convention:

- *"every other user except the speaker"* — the core never routes an
  utterance to its own speaker, and their feed simply never carries it;
- *per-listener languages* — each feed can carry different audio;
- *audio-only clients* — the bridge subscribes to `KIND_AUDIO` only; a peer
  with no camera is a fully supported participant.

## Relay modes

| Mode | Behavior |
|---|---|
| `ENGLISH_RELAY` (default) | Non-English utterances are translated to English and spoken to all other participants. English speech is not relayed (everyone understood it). |
| `NATIVE_RELAY` | Every utterance is translated to **each listener's own language** and spoken on that listener's feed. Listeners who share the speaker's language are skipped — they heard the original. |

The mode is switched at runtime with a data message on topic
`rs.translation.control`:

```json
{"mode": "NATIVE_RELAY"}
```

## Language tracking

The registry (`translation/registry.py`) is the authoritative per-user language
map, updated on every finalized utterance:

- `detected_language` — latest STT-reported language (normalized across
  provider aliases: `cmn-hans`/`zh-cn` → `zh`, `fil` → `tl`, …).
- `preferred_language` — an explicit declaration that overrides detection for
  messages spoken *to* that user. Sent by the client on join:

```json
// topic rs.translation.control
{"preferred_language": "es"}
```

A `NATIVE_RELAY` listener with no known language yet hears the configured
fallback (`translation_fallback_language`, default English) — after their
first utterance, or a declaration, they hear their own language.

## Chat / transcript surface

Every utterance and every relayed delivery is surfaced twice:

1. **Live** — a data packet on topic `rs.translation.chat`, addressed to the
   delivery's target only:

   ```json
   {
     "id": "…", "from": "user-es", "from_language": "es",
     "text": "hola",
     "to": "user-zh", "to_language": "zh",
     "translation": "你好", "mode": "NATIVE_RELAY"
   }
   ```

   The mobile app renders these as chat bubbles (client contract: subscribe to
   the topic; subscribe only to your own `translation-<identity>` audio track).
2. **Durable** — via `TranscriptSink` (originals as `translation_stt`, relays
   as `translation_relay` with the language field), when the job metadata
   carries a `session_id` the backend knows about.

## Configuration (agent/.env)

| Setting | Default | Notes |
|---|---|---|
| `TRANSLATION_STT_MODEL` | `assemblyai/universal-streaming-multilingual` | provider-side detection supplies language codes. Do NOT use `deepgram/nova-3:multi`: its multi subset excludes Chinese, and this gateway rejects `detect_language` on it. |
| `TRANSLATION_RELAY_MODE` | `ENGLISH_RELAY` | starting mode; live-switchable |
| `TRANSLATION_TTS_BACKEND` | `edge` | `edge` = edge-tts neural voices (network required, verified for en/zh/es/tl). `livekit` resolves through Cloud Inference, but Inference TTS currently emits no audio frames in this project — the relay would be silence. |
| `TRANSLATION_TTS_VOICES` | `es=deepgram/aura-2:athena` | `lang=provider/model:voice` map for the `livekit` backend; missing langs fall back to `TTS_MODEL` |
| `TRANSLATION_FALLBACK_LANGUAGE` | `en` | unknown-listener language in NATIVE_RELAY |

When the STT reports no usable language code (AssemblyAI's multilingual
model sometimes tags the first final of an utterance `en`), the translator
runs a constrained detection pass (en/zh/es/tl only) before translating.

Relay latency budget, measured on LiveKit Cloud: STT final ~10–20 s after
the utterance ends, LLM translation ~5–30 s, edge-tts ~5–10 s — plan for
30–60 s between a speaker finishing a sentence and listeners hearing the
translation.

## Client app: translation_mobile/

A purpose-built Flutter client (`translation_mobile/`, Android-first) with
17 widget/unit tests (`flutter test`):

- publishes **microphone only** — audio-only participants, no camera;
- connects with `autoSubscribe=false` and subscribes ONLY to its own
  `translation-<identity>` feed — it never hears another participant's
  relay or its own;
- renders `rs.translation.chat` packets as bilingual bubbles (translation
  + original) and plays the relay feed via the SDK's audio pipeline;
- declares its preferred language on join and can switch the room relay
  mode from the call screen;
- joins via deep link, which is how the emulator test harness drives it:

```
adb shell am start -a android.intent.action.VIEW \
  -d "translationmobile://join?url=<wss-url>&token=<jwt>&identity=user-zh&lang=zh" \
  -n dev.remotesupport.translation_mobile/.MainActivity
```

iOS: not buildable on this machine (no Xcode). The app code is
platform-neutral; `flutter create --platforms=ios` plus `pod install` in a
macOS + Xcode environment is the remaining step.

## Live end-to-end test

`scripts/translation-live-test.py` runs the whole pipeline against real
LiveKit Cloud: creates a room, dispatches `translation-agent`, joins a
synthetic Spanish-speaking peer (edge-tts audio published over real
WebRTC), records the `translation-user-zh` feed — exactly what the app
user hears — and re-transcribes it with local whisper, matching against
the corpus references. `--reuse` re-runs against the room/tokens in
`/tmp/translation_live_test.json` (the script recycles the room to evict
stale agents). Requires the worker running and `agent/.env` credentials.

Verified 2026-10-17: `LIVE TEST PASS` — the recorded app feed transcribed
to "screen broke and the touch isn't working." matching reference sentence
15, with the app simultaneously showing the bilingual chat bubble.


All models resolve through LiveKit Cloud Inference with `LIVEKIT_API_KEY` —
same one-vendor policy as the support agent (NFR-6).

## Testing: synthetic loop-back harness

`tests/test_translation_loopback.py` (+ `tests/translation_corpus.py`) verify
detection → transcription → translation → speech synthesis → **re-transcription
of the spoken relay** end to end, with no humans in the loop:

1. **Corpus**: 25 aligned support-call sentences in each of en/zh/es/tl —
   `SENTENCES[lang][i]` is the faithful translation of `SENTENCES["en"][i]`.
2. **Speech generation**: edge-tts neural voices (incl. `fil-PH` for Tagalog;
   macOS `say` has no Tagalog voice), decoded to PCM16 via ffmpeg.
3. **Recognition**: local faster-whisper with auto language detection.
4. **Loop-back**: for every sentence — synthesize, transcribe, assert detected
   language and transcript; run through `TranslationCore`; re-transcribe each
   delivery's audio and fuzzy-compare against the reference translation the
   other synthetic user should hear. Includes a multi-turn es ↔ zh
   conversation in `NATIVE_RELAY` and English relays for zh/es/tl.
5. **Live LLM check**: when `LIVEKIT_API_KEY`/`SECRET` are set, translates
   corpus samples through the real Inference LLM and compares against the
   references; skipped otherwise (the phrase-table translator keeps the
   pipeline verification deterministic and offline).

Requires network (one-time whisper model download + edge-tts) and ffmpeg.
Deselect with `pytest -m "not slow"`. Useful env overrides:
`TRANSLATION_TEST_WHISPER_MODEL` (default `small`),
`TRANSLATION_TEST_SIMILARITY` (default `0.6`).

## Known limits

- Interim transcripts are discarded (same policy as FR-6.3): relay happens on
  finalized utterances, so there is a one-turn-fragment lag, like a human
  interpreter finishing a sentence.
- Unsupported languages pass through the registry untranslated-code-wise and
  are relayed via the LLM anyway (it can translate them); detection accuracy
  for out-of-focus languages is untested.
- Simultaneous speakers on one mic stream degrade STT finals; the pipeline is
  per-participant, so cross-participant overlap is handled independently.
