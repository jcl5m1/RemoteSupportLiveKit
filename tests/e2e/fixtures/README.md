# E2E test fixtures

## `caller_prompt.wav`

A short PCM WAV (16 kHz, mono, 16-bit) used by `test_caller_speech_agent_response`.
The browser loads this file and publishes it as the caller's audio track so the
agent's STT pipeline can transcribe real speech and the LLM can generate a
response.

### Regenerating on macOS

```bash
say -v Samantha "Hello agent, what is the current time?" -o caller_prompt.aiff
ffmpeg -y -i caller_prompt.aiff -ar 16000 -ac 1 -c:a pcm_s16le caller_prompt.wav
rm caller_prompt.aiff
```

### Regenerating on Linux

```bash
espeak-ng "Hello agent, what is the current time?" -w caller_prompt.wav
ffmpeg -y -i caller_prompt.wav -ar 16000 -ac 1 -c:a pcm_s16le caller_prompt.wav.tmp
mv caller_prompt.wav.tmp caller_prompt.wav
```

The exact phrase can be changed, but the test only asserts that the agent
transcribes *some* caller speech and replies, not that it matches specific words.
