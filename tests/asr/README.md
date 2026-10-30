# ASR benchmark (synthetic speech)

**Results and recommendation: [ANALYSIS.md](ANALYSIS.md).** The hardened
benchmark (Taglish, phone-line audio, real recordings, cloud vs local, batch vs
streaming) is run with `make_harder.py`, `make_real.py`, `bench_cloud.py`,
`bench_gpu.py`, `bench_stream.py`, then `analyze_v2.py` / `analyze_stream.py`.

Word/character error rates of speech-recognition models on synthetic,
work-related speech in the call languages, to choose the transcription model.

| Step | Script | Output |
|---|---|---|
| 1. 100 deterministic phrases per language | `generate_phrases.py` | `phrases.json` |
| 2. Speak them with edge-tts neural voices | `synthesize.py` | `audio/<lang>/*.mp3`, `dataset.csv` |
| 3. Transcribe and score | `eval.py` | `transcriptions.csv`, `results.csv`, `asr_benchmark_wer_cer.png` |
| 3b. Add Qwen3-ASR-1.7B (own venv: it pins transformers 4.57) | `eval_qwen.py` | rows merged into `transcriptions.csv`; re-run `eval.py` to re-score |
| 4. Grouped chart | `plot_by_language.py` | `asr_benchmark_by_language.png` |

Languages: English, Mandarin (mainland and Taiwan), Spanish, Tagalog. Models:
Whisper Large v3 Turbo and Whisper Medium (faster-whisper) for all, plus
Paraformer-zh (FunASR) for Mandarin and Wav2Vec2-XLSR-Spanish for Spanish,
and Qwen3-ASR-1.7B for all.

The audio is checked in so results can be reproduced without the edge-tts
service (it doesn't always return audio, e.g. for Tagalog from CI runners).

```bash
cd tests/asr
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python eval.py          # downloads the models on first run
```

Not part of CI (model downloads and CPU inference take a long time).
