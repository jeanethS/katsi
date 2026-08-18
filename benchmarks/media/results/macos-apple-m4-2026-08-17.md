# Local media-adapter benchmark — macOS / Apple M4

Run date: 2026-08-17

## Environment

- macOS 26.2, arm64; Apple M4 (10 cores), 24 GB memory.
- `ffmpeg` 8.1.2 with VideoToolbox and NEON enabled.
- The Ollama CLI is installed, but its local service was unavailable.
- `tesseract` and Python packages for OCR, transcription, captioning, and visual embedding were unavailable.

## Measured local stages

All measurements used a generated 3-second, 1280×720 H.264/AAC video and
`/usr/bin/time -l`; no original workspace media was used.

| Stage | Adapter | Wall time | Peak RSS | Result |
| --- | --- | ---: | ---: | --- |
| Content detection | `ContentSignatureDetector` | 120 ms | 42.2 MB | `video/mp4` detected |
| Audio extraction/normalization | FFmpeg 8.1.2 | 20 ms | 19.4 MB | 16 kHz mono WAV created |
| Keyframe sampling | FFmpeg 8.1.2 | 60 ms | 80.8 MB | Three PNG frames created at 1 fps |

The complete generated output used 2.6 MB on disk.

## End-to-end adapter measurements

The fixtures are synthetic and have known transcripts. Measurements use a
single cold run, so they are suitability signals rather than release gates.

| Capability | Adapter | Quality | Wall time | Peak RSS | Result |
| --- | --- | ---: | ---: | ---: | --- |
| OCR | Tesseract 5.5.3 | 0.887 character accuracy | 100 ms | 62.5 MB | English fixture recognized; first line was truncated |
| Transcription | whisper.cpp 1.9.2 + `ggml-base` | 0.20 WER | 620 ms | 337.5 MB | Metal accelerated, 2.3 s English clip |
| Caption / visible-text reading | Ollama `qwen2.5vl:7b` | exact fixture text | — | — | local vision model produced both lines |

The Tesseract and Whisper runs produced no network traffic. The caption model
was also served by the local Ollama process. Visual embeddings remain
unavailable: `bge-m3` is a text encoder, so it must not be treated as a visual
embedding adapter.

## Availability and selected defaults

- **Detector:** select `ContentSignatureDetector`; it is deterministic, local,
  and requires no optional runtime.
- **Decode, audio-track extraction, and keyframe sampling:** select FFmpeg
  8.1.2 when its availability probe passes.
- **OCR:** Tesseract is available for English, but remains opt-in until the
  owner chooses an output-wrapper contract and accepts the measured accuracy.
- **Transcription:** whisper.cpp is available with the local multilingual base
  model and Metal acceleration; it remains opt-in pending a larger corpus.
- **Captioning:** the local `qwen2.5vl:7b` model is available through Ollama;
  it remains opt-in pending bounded structured-output adapter configuration.
- **Scene detection and visual embedding:** no compatible local adapter is
  configured, so both remain unavailable rather than falling back to remote
  processing.

This is a single-machine benchmark. Locator-quality evaluation and comparative
visual-embedding benchmarks remain pending until a region-aware OCR wrapper
and a compatible local visual encoder are configured.
