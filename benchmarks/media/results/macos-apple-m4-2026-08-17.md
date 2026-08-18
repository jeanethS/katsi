# Local media-adapter benchmark — macOS / Apple M4

Run date: 2026-08-17

## Environment and method

- macOS 26.2, arm64; Apple M4 (10 cores), 24 GB memory.
- FFmpeg 8.1.2; Tesseract 5.5.3; whisper.cpp 1.9.2 with `ggml-base`;
  Ollama `qwen2.5vl:7b` and `bge-m3`.
- Fixtures were generated locally: a 1200x400 SVG with known text, a 2.253 s
  16 kHz mono speech clip produced with `say`, and an H.264/AAC video that
  combines them. No workspace media or network service was used.
- Wall time and process RSS come from `/usr/bin/time -l`; all measurements are
  one run. The Ollama CLI RSS is not the model footprint, so its GPU residency
  is taken from `ollama ps` separately.

## Results

| Capability | Adapter | Accuracy / locator quality | Wall | Peak memory | Derived disk |
| --- | --- | --- | ---: | ---: | ---: |
| Detection | `ContentSignatureDetector` | Correct `video/mp4`, no extension mismatch | 180 ms | 12.0 MiB | <4 KiB descriptor |
| OCR | Tesseract | 0.868 character, 0.875 word accuracy; all 8/8 TSV word boxes were non-empty and inside the 1280x1280 raster | 120 ms | 57.7 MiB | <4 KiB text + TSV |
| Transcription (Metal) | whisper.cpp `ggml-base` | 0.20 WER, 0.086 CER; timestamp range 0–2240 ms versus a 0–2253 ms clip (0.994 temporal IoU) | 7.47 s cold | 366.2 MiB | <4 KiB transcript |
| Transcription fallback | whisper.cpp `--no-gpu` | Same 0.20 WER on the fixture; proves CPU fallback executes | 850 ms warm | 340.0 MiB | <4 KiB transcript |
| Caption / visible-text reading | Ollama `qwen2.5vl:7b` | Exact visible text after whitespace normalization; whole-resource locator only | 24.68 s | 5.6 GiB GPU-resident model; 18.8 MiB CLI client | <4 KiB text |
| Audio extraction | FFmpeg | 2.304 s, 16 kHz mono WAV from the 2.253 s source | 20 ms | 7.0 MiB | 76 KiB |
| Keyframe sampling | FFmpeg at 1 fps | 2 keyframes span the full 2.253 s source | 50 ms | 62.4 MiB | 316 KiB |
| Scene detection | FFmpeg `scene` filter | No boundaries for the synthetic continuous test pattern; bounded fallback is `[0, 2253]` ms | 80 ms | 49.8 MiB | <4 KiB |

The output sizes above total about 400 KiB for the useful derived artifacts;
the source video is 864 KiB. The temporary benchmark directory, including
logs and intermediate rasters, occupied 1.4 MiB.

## Availability and defaults

- **Select:** `ContentSignatureDetector` and FFmpeg for deterministic
  metadata, decode, audio extraction, scene detection, and keyframes when
  their availability probes pass.
- **Keep opt-in:** Tesseract (the fixture truncates the rendered first line),
  whisper.cpp (one tiny English clip is insufficient), and Qwen-VL (24.68 s
  latency and a 5.6 GiB GPU model footprint). The Whisper CPU run is a
  functional fallback, not a cold-start comparison with Metal.
- **Do not enable:** visual embeddings. `bge-m3` advertises text embedding
  only, while Qwen-VL advertises vision completion only; neither may be
  represented as a compatible visual encoder.
- **No remote fallback:** every measured adapter stayed local. Qwen-VL used
  the local Ollama process; `ollama ps` reported 100% GPU placement.

These values are suitability signals for this hardware, not CI thresholds.
Any default change requires an owner-configured pipeline and a broader
licensed corpus covering the supported languages and media classes.
