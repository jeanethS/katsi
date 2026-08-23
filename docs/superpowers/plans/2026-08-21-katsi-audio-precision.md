# Katsi Audio Precision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give katsi frame-accurate speech timings and measured silence spans, so a consumer can locate an exact moment in audio or video instead of a multi-second window.

**Architecture:** Two independent additions to the existing audio pipeline module. Word timings extend the transcription JSON contract and its parser, riding inside the transcript segment representation. Silence gets its own ffmpeg-backed pipeline and representation kind, kept separate from the transcript stream because ffmpeg is `DETERMINISTIC` while whisper is `MODEL_BACKED`, and katsi's provenance model must not blur the two.

**Tech Stack:** Python 3.12+, pydantic, Kùzu (graph), pytest. ffmpeg and a whisper wrapper are invoked only through `BoundedSubprocessExecutor` under owner-authored `MediaPipelineDefinition` objects — never as raw subprocess calls.

**Spec:** `docs/superpowers/specs/2026-08-21-katsi-audio-precision-design.md`

## Global Constraints

- Katsi answers *where and when*. No beat detection, energy envelopes, cut points, or EDL logic enters this codebase.
- Every subprocess runs through `BoundedSubprocessExecutor` with `network_disabled=True` and `strict_output_contract=True`. Never call `subprocess` directly from a pipeline.
- Parsers never fabricate or silently repair. Any structural problem raises `ValueError`. This mirrors the existing convention in `media/audio_pipeline.py`.
- Non-speech segments carry neither text nor words.
- `MediaProcessingConfig.enable_audio_processing` (`packages/core/katsi_core/media/contracts.py:734`) stays `default=False`. New pipelines are not added to the default registry. Existing workspaces must see zero behavior change.
- Silence detection defaults: threshold `-35dB`, minimum duration `250ms`. Both are parameters on the definition builder, not module constants.
- All paths below are relative to `/Users/jeanhrdz/katsi`.

---

### Task 1: Word-level timings on transcript segments

**Files:**
- Modify: `packages/core/katsi_core/media/audio_pipeline.py` (add `WordTimingData` near `TranscriptSegmentData` at `:448`; extend `TranscriptSegmentData`; extend `parse_transcript_segments` at `:607`; extend `build_transcribe_definition` at `:461`)
- Test: `tests/test_media_audio_pipeline.py` (add to existing `class TestTranscriptSegmentParsing` at `:292`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `WordTimingData(start_ms: int, end_ms: int, text: str, confidence: float | None)`, and `TranscriptSegmentData.words: tuple[WordTimingData, ...]` defaulting to `()`.

**Background the implementer needs:** `build_transcribe_definition` does not call the stock whisper CLI. It demands JSON with keys `segments` and `coverage_fraction`, which stock whisper never emits — the configured executable is an owner-supplied wrapper. This task extends the contract that wrapper must satisfy. The wrapper is what passes `--word_timestamps True` to whisper underneath; that is deployment configuration, not code in this repo.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_audio_pipeline.py` inside `class TestTranscriptSegmentParsing`:

```python
    def test_parses_word_timings_within_segment(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 1000,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "hello world",
                    "words": [
                        {"start_ms": 1000, "end_ms": 1500, "text": "hello", "confidence": 0.9},
                        {"start_ms": 1600, "end_ms": 2400, "text": "world"},
                    ],
                }
            ],
        }

        segments, _ = parse_transcript_segments(batch)

        assert [w.text for w in segments[0].words] == ["hello", "world"]
        assert segments[0].words[0].start_ms == 1000
        assert segments[0].words[0].confidence == 0.9
        assert segments[0].words[1].confidence is None

    def test_segment_without_words_key_parses_with_empty_words(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {"start_ms": 0, "end_ms": 1000, "segment_kind": "speech", "text": "hi"}
            ],
        }

        segments, _ = parse_transcript_segments(batch)

        assert segments[0].words == ()

    def test_word_outside_parent_segment_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "segment_kind": "speech",
                    "text": "hi",
                    "words": [{"start_ms": 1000, "end_ms": 2500, "text": "hi"}],
                }
            ],
        }

        with pytest.raises(ValueError, match="within its segment"):
            parse_transcript_segments(batch)

    def test_out_of_order_words_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "a b",
                    "words": [
                        {"start_ms": 1500, "end_ms": 2000, "text": "b"},
                        {"start_ms": 100, "end_ms": 500, "text": "a"},
                    ],
                }
            ],
        }

        with pytest.raises(ValueError, match="time-ordered"):
            parse_transcript_segments(batch)

    def test_overlapping_words_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "a b",
                    "words": [
                        {"start_ms": 100, "end_ms": 1200, "text": "a"},
                        {"start_ms": 1000, "end_ms": 2000, "text": "b"},
                    ],
                }
            ],
        }

        with pytest.raises(ValueError, match="time-ordered"):
            parse_transcript_segments(batch)

    def test_zero_length_word_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "a",
                    "words": [{"start_ms": 100, "end_ms": 100, "text": "a"}],
                }
            ],
        }

        with pytest.raises(ValueError, match="must exceed"):
            parse_transcript_segments(batch)

    def test_non_speech_segment_with_words_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "segment_kind": "silence",
                    "text": "",
                    "words": [{"start_ms": 0, "end_ms": 500, "text": "ghost"}],
                }
            ],
        }

        with pytest.raises(ValueError, match="must not carry words"):
            parse_transcript_segments(batch)

    def test_word_confidence_out_of_range_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "a",
                    "words": [
                        {"start_ms": 100, "end_ms": 500, "text": "a", "confidence": 1.5}
                    ],
                }
            ],
        }

        with pytest.raises(ValueError, match="confidence"):
            parse_transcript_segments(batch)
```

Add `WordTimingData` to the existing `from katsi_core.media.audio_pipeline import (...)` block at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_media_audio_pipeline.py::TestTranscriptSegmentParsing -v`
Expected: FAIL — `ImportError: cannot import name 'WordTimingData'`

- [ ] **Step 3: Add the `WordTimingData` type**

In `packages/core/katsi_core/media/audio_pipeline.py`, immediately above `TranscriptSegmentData` (currently at `:448`):

```python
@dataclass(frozen=True, slots=True)
class WordTimingData:
    """One word-level timing inside a speech segment (7.3).

    Rides inside its parent transcript segment; never its own
    representation, graph node, or embedding -- a single word is not
    independently meaningful to retrieve.
    """

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
```

- [ ] **Step 4: Add the `words` field**

In the same file, add to `TranscriptSegmentData` as the last field so existing positional construction is unaffected:

```python
    words: tuple[WordTimingData, ...] = ()
```

- [ ] **Step 5: Add the word parser helper**

Add above `parse_transcript_segments` (currently at `:607`):

```python
def _parse_word_timings(
    raw_segment: dict[str, Any], *, segment_start_ms: int, segment_end_ms: int, segment_kind: str
) -> tuple[WordTimingData, ...]:
    """Strictly parse optional word-level timings for one segment.

    The array is optional so transcription wrappers written against the
    pre-word contract keep working: they simply produce no words.
    """
    raw_words = raw_segment.get("words")
    if raw_words is None:
        return ()
    if not isinstance(raw_words, list):
        raise ValueError("Segment words must be a JSON array")
    # 7.6 parity with text: non-speech segments never carry fabricated words.
    if segment_kind != "speech" and raw_words:
        raise ValueError(f"Non-speech segment_kind={segment_kind!r} must not carry words")

    words: list[WordTimingData] = []
    previous_end_ms = segment_start_ms
    for raw_word in raw_words:
        if not isinstance(raw_word, dict):
            raise ValueError("Each word must be a JSON object")

        start_ms = int(raw_word["start_ms"])
        end_ms = int(raw_word["end_ms"])
        if end_ms <= start_ms:
            raise ValueError(f"Word end_ms ({end_ms}) must exceed start_ms ({start_ms})")
        if start_ms < segment_start_ms or end_ms > segment_end_ms:
            raise ValueError(
                f"Word range ({start_ms}, {end_ms}) must fall within its segment "
                f"({segment_start_ms}, {segment_end_ms})"
            )
        if start_ms < previous_end_ms:
            raise ValueError("Words must be time-ordered and non-overlapping")
        previous_end_ms = end_ms

        text = raw_word["text"]
        if not isinstance(text, str):
            raise ValueError("Word text must be a string")

        confidence = raw_word.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not (0.0 <= confidence <= 1.0):
                raise ValueError("Word confidence must be within [0.0, 1.0]")

        words.append(
            WordTimingData(start_ms=start_ms, end_ms=end_ms, text=text, confidence=confidence)
        )

    return tuple(words)
```

Note the ordering check uses `start_ms < previous_end_ms` seeded from `segment_start_ms`, which rejects out-of-order and overlapping words in one comparison.

- [ ] **Step 6: Wire the helper into `parse_transcript_segments`**

In `parse_transcript_segments`, replace the `segments.append(...)` call (currently at `:647-656`) with:

```python
        words = _parse_word_timings(
            raw_segment,
            segment_start_ms=start_ms,
            segment_end_ms=end_ms,
            segment_kind=segment_kind,
        )

        segments.append(
            TranscriptSegmentData(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                confidence=confidence,
                segment_kind=segment_kind,
                language=raw_segment.get("language"),
                words=words,
            )
        )
```

- [ ] **Step 7: Request words in the transcription contract**

In `build_transcribe_definition` (`:461`), change `fixed_args` to:

```python
        fixed_args=["{input_path}", "--output_dir", "{working_directory}", "--word-timestamps"],
```

Then extend the docstring to record what the wrapper must now emit:

```python
    """Owner-configured definition for local speech transcription.

    The configured executable is an owner-supplied wrapper, not the stock
    whisper CLI: the strict contract requires ``segments`` and
    ``coverage_fraction``, which stock whisper does not emit. With
    ``--word-timestamps`` the wrapper must additionally emit an optional
    per-segment ``words`` array of ``{start_ms, end_ms, text, confidence?}``,
    each word's range falling within its parent segment, time-ordered and
    non-overlapping. Speech segments only.
    """
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `pytest tests/test_media_audio_pipeline.py::TestTranscriptSegmentParsing -v`
Expected: PASS, including the pre-existing tests in that class.

- [ ] **Step 9: Run the full audio suite for regressions**

Run: `pytest tests/test_media_audio_pipeline.py -v`
Expected: PASS. If a pre-existing test asserts on `build_transcribe_definition.fixed_args`, update it to include `--word-timestamps` — that is an intended contract change, not a regression.

- [ ] **Step 10: Commit**

```bash
git add packages/core/katsi_core/media/audio_pipeline.py tests/test_media_audio_pipeline.py
git commit -m "feat: parse word-level timings on speech transcript segments

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Parse ffmpeg silencedetect output

**Files:**
- Modify: `packages/core/katsi_core/media/audio_pipeline.py` (add `SilenceSpanData` and `parse_silence_spans` after the decode section, before the 7.3 transcription section header at `:440`)
- Test: `tests/test_media_audio_pipeline.py` (add new `class TestSilenceSpanParsing`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SilenceSpanData(start_ms: int, end_ms: int)` and `parse_silence_spans(stderr: str, *, track_duration_ms: int) -> list[SilenceSpanData]`.

**Background the implementer needs:** ffmpeg's `silencedetect` filter writes to stderr, in lines shaped like `[silencedetect @ 0x...] silence_start: 1.234` and `[silencedetect @ 0x...] silence_end: 5.678 | silence_duration: 4.444`. Times are in **seconds as floats**; katsi stores integer milliseconds everywhere. If the track ends while still silent, ffmpeg emits a `silence_start` with no matching `silence_end` — that span must close at `track_duration_ms` rather than being dropped, because trailing dead air is exactly what a consumer wants to trim.

- [ ] **Step 1: Write the failing tests**

Add a new class to `tests/test_media_audio_pipeline.py`:

```python
class TestSilenceSpanParsing:
    def test_parses_paired_silence_spans(self):
        stderr = (
            "[silencedetect @ 0x7f8] silence_start: 1.5\n"
            "[silencedetect @ 0x7f8] silence_end: 3.25 | silence_duration: 1.75\n"
            "[silencedetect @ 0x7f8] silence_start: 10.0\n"
            "[silencedetect @ 0x7f8] silence_end: 12.5 | silence_duration: 2.5\n"
        )

        spans = parse_silence_spans(stderr, track_duration_ms=20_000)

        assert [(s.start_ms, s.end_ms) for s in spans] == [(1500, 3250), (10_000, 12_500)]

    def test_trailing_silence_closes_at_track_duration(self):
        stderr = "[silencedetect @ 0x7f8] silence_start: 8.0\n"

        spans = parse_silence_spans(stderr, track_duration_ms=10_000)

        assert [(s.start_ms, s.end_ms) for s in spans] == [(8000, 10_000)]

    def test_no_silence_produces_no_spans(self):
        spans = parse_silence_spans("frame= 100 fps=0.0\n", track_duration_ms=5000)

        assert spans == []

    def test_ignores_unrelated_ffmpeg_stderr_noise(self):
        stderr = (
            "ffmpeg version 6.0 Copyright (c) 2000-2023\n"
            "  Duration: 00:00:20.00, start: 0.000000, bitrate: 256 kb/s\n"
            "[silencedetect @ 0x7f8] silence_start: 1.0\n"
            "[silencedetect @ 0x7f8] silence_end: 2.0 | silence_duration: 1.0\n"
        )

        spans = parse_silence_spans(stderr, track_duration_ms=20_000)

        assert [(s.start_ms, s.end_ms) for s in spans] == [(1000, 2000)]

    def test_silence_end_without_start_raises(self):
        stderr = "[silencedetect @ 0x7f8] silence_end: 2.0 | silence_duration: 1.0\n"

        with pytest.raises(ValueError, match="silence_end without"):
            parse_silence_spans(stderr, track_duration_ms=5000)

    def test_trailing_silence_beyond_track_duration_raises(self):
        stderr = "[silencedetect @ 0x7f8] silence_start: 12.0\n"

        with pytest.raises(ValueError, match="exceeds track duration"):
            parse_silence_spans(stderr, track_duration_ms=10_000)

    def test_zero_length_span_rejected(self):
        stderr = (
            "[silencedetect @ 0x7f8] silence_start: 2.0\n"
            "[silencedetect @ 0x7f8] silence_end: 2.0 | silence_duration: 0.0\n"
        )

        with pytest.raises(ValueError, match="must exceed"):
            parse_silence_spans(stderr, track_duration_ms=5000)
```

Add `SilenceSpanData` and `parse_silence_spans` to the `katsi_core.media.audio_pipeline` import block at the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_media_audio_pipeline.py::TestSilenceSpanParsing -v`
Expected: FAIL — `ImportError: cannot import name 'parse_silence_spans'`

- [ ] **Step 3: Implement the parser**

Add to `packages/core/katsi_core/media/audio_pipeline.py`, before the `# 7.3 Configured local speech transcription` section header at `:440`:

```python
# =============================================================================
# 7.2b Measured silence spans (deterministic, ffmpeg silencedetect)
# =============================================================================


_SILENCE_START_PATTERN = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_PATTERN = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


@dataclass(frozen=True, slots=True)
class SilenceSpanData:
    """One measured span of silence in a normalized audio track (7.2b).

    Deterministic: produced by ffmpeg's silencedetect filter, never by a
    model. Carries no text -- silence is positionally useful, not
    semantically retrievable.
    """

    start_ms: int
    end_ms: int


def _seconds_to_ms(raw: str) -> int:
    return int(round(float(raw) * 1000))


def parse_silence_spans(stderr: str, *, track_duration_ms: int) -> list[SilenceSpanData]:
    """Strictly parse ffmpeg silencedetect stderr into measured spans.

    A trailing ``silence_start`` with no matching ``silence_end`` means the
    track ended while still silent; that span closes at ``track_duration_ms``
    rather than being dropped. Never fabricates: any structurally impossible
    pairing raises rather than being silently repaired.
    """
    spans: list[SilenceSpanData] = []
    open_start_ms: int | None = None

    for line in stderr.splitlines():
        start_match = _SILENCE_START_PATTERN.search(line)
        if start_match:
            if open_start_ms is not None:
                raise ValueError("Received silence_start while a previous span was still open")
            open_start_ms = _seconds_to_ms(start_match.group(1))
            continue

        end_match = _SILENCE_END_PATTERN.search(line)
        if end_match:
            if open_start_ms is None:
                raise ValueError("Received silence_end without a preceding silence_start")
            end_ms = _seconds_to_ms(end_match.group(1))
            if end_ms <= open_start_ms:
                raise ValueError(
                    f"Silence end_ms ({end_ms}) must exceed start_ms ({open_start_ms})"
                )
            spans.append(SilenceSpanData(start_ms=open_start_ms, end_ms=end_ms))
            open_start_ms = None

    if open_start_ms is not None:
        if open_start_ms >= track_duration_ms:
            raise ValueError(
                f"Trailing silence_start ({open_start_ms}) exceeds track duration "
                f"({track_duration_ms})"
            )
        spans.append(SilenceSpanData(start_ms=open_start_ms, end_ms=track_duration_ms))

    return spans
```

Add `import re` to the module's import block if it is not already present.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_media_audio_pipeline.py::TestSilenceSpanParsing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/core/katsi_core/media/audio_pipeline.py tests/test_media_audio_pipeline.py
git commit -m "feat: parse ffmpeg silencedetect output into measured spans

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Silence detection pipeline

**Files:**
- Modify: `packages/core/katsi_core/media/contracts.py` (add `PipelineStage.DETECT_SILENCE` after `:119`; add `MediaRepresentationKind.SILENCE_SPAN` after `:56`)
- Modify: `packages/core/katsi_core/media/audio_pipeline.py` (add `build_silence_detect_definition` and `AudioSilenceDetectionPipeline` after `parse_silence_spans`)
- Test: `tests/test_media_audio_pipeline.py` (add new `class TestAudioSilenceDetectionPipeline`)

**Interfaces:**
- Consumes: `SilenceSpanData` and `parse_silence_spans` from Task 2.
- Produces:
  - `build_silence_detect_definition(*, executable_path="ffmpeg", noise_threshold_db=-35.0, min_silence_ms=250, timeout_seconds=60.0, max_output_bytes=20_000_000) -> MediaPipelineDefinition`
  - `AudioSilenceDetectionPipeline`, whose `process(...)` returns a **single** `DerivedRepresentation` of kind `SILENCE_SPAN` carrying the parsed span batch as its textual payload.
  - `build_silence_span_representations(spans: list[SilenceSpanData], resource_version_id: ResourceVersionId, pipeline_fingerprint: PipelineFingerprint, adapter: ProducerProvenance, *, track_duration_ms: int) -> list[DerivedRepresentation]`

**Background the implementer needs:** Model this on `AudioDecodePipeline` (`:340`) and `AudioTranscriptionPipeline` (`:509`) — same executor, same definition discipline.

Three things matter. First, this pipeline consumes the normalized WAV proxy that decode already produced, so it declares `input_kinds=[MediaRepresentationKind.PROXY_MEDIA]` and does no decoding of its own.

Second, `silencedetect` produces its findings on **stderr**, not a file, so the pipeline reads `result.stderr_sample`. `BoundedSubprocessExecutor` truncates that at `max_output_bytes` and sets `result.output_truncated` — truncated output must raise, never be parsed, because a truncated span list silently loses real silence.

Third, and easy to get wrong: **`process` returns one representation, not N.** This module's established shape is a single-valued `process` carrying a validated batch, plus a separate `build_*_representations` expander that fans it out — see `AudioTranscriptionPipeline.process` alongside `build_segment_representations` (`:669`). The orchestrator and representation cache key one representation per pipeline fingerprint, so returning a list from `process` would break caching. Follow the existing pattern exactly.

`silencedetect` needs the track duration to close a trailing span and to compute coverage. Take it from the caller rather than re-probing: both `process` and the expander accept a `track_duration_ms` keyword.

`TimeRangeLocator` is already imported in this module (used at `:690`); no import change needed.

- [ ] **Step 1: Write the failing tests**

Add a new class to `tests/test_media_audio_pipeline.py`:

Note the `fingerprint` fixture below: `pipeline_fingerprint` is a structured `PipelineFingerprint` object, never a string. The neighbouring audio tests build one with `build_pipeline_fingerprint(...)`; this fixture does the same.

```python
@pytest.fixture
def fingerprint(source_content_hash):
    return build_pipeline_fingerprint(
        source_content_hash=source_content_hash,
        representation_kind=MediaRepresentationKind.SILENCE_SPAN,
        stage=PipelineStage.DETECT_SILENCE,
        adapter_name="audio_silence_detect_ffmpeg",
        adapter_version="1.0.0",
        settings=MediaSamplingSettings(),
    )


class TestAudioSilenceDetectionPipeline:
    def _fake_silence_definition(self, tmp_path, stderr_text):
        # A safe stand-in for ffmpeg: a python3 script that emits canned
        # silencedetect lines on stderr, proving the adapter only ever routes
        # through BoundedSubprocessExecutor with a fixed template.
        script = tmp_path / "fake_silencedetect.py"
        script.write_text(
            "import sys\nsys.stderr.write(sys.argv[1])\n"
        )
        return build_silence_detect_definition().model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": [str(script), stderr_text],
            }
        )

    def test_process_returns_single_batch_representation(
        self, resource_version_id, source_content_hash, fingerprint, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())
        stderr_text = (
            "[silencedetect @ 0x1] silence_start: 1.0\n"
            "[silencedetect @ 0x1] silence_end: 2.0 | silence_duration: 1.0\n"
        )

        adapter = AudioSilenceDetectionPipeline(
            self._fake_silence_definition(tmp_path, stderr_text)
        )
        representation = adapter.process(
            input_path,
            resource_version_id,
            source_content_hash,
            fingerprint,
            tmp_path,
            track_duration_ms=10_000,
        )

        # One representation carrying the batch, mirroring
        # AudioTranscriptionPipeline -- expansion is the expander's job.
        assert isinstance(representation, DerivedRepresentation)
        assert representation.kind == MediaRepresentationKind.SILENCE_SPAN
        assert representation.status == MediaRepresentationStatus.CURRENT
        batch = json.loads(representation.textual_payload)
        assert batch["spans"] == [{"start_ms": 1000, "end_ms": 2000}]
        assert batch["track_duration_ms"] == 10_000

    def test_truncated_output_refuses_to_parse(
        self, resource_version_id, source_content_hash, fingerprint, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())
        stderr_text = "[silencedetect @ 0x1] silence_start: 1.0\n"

        definition = self._fake_silence_definition(tmp_path, stderr_text).model_copy(
            update={"max_output_bytes": 8}
        )
        adapter = AudioSilenceDetectionPipeline(definition)

        with pytest.raises(RuntimeError, match="truncated"):
            adapter.process(
                input_path,
                resource_version_id,
                source_content_hash,
                fingerprint,
                tmp_path,
                track_duration_ms=10_000,
            )

    def test_expander_produces_one_representation_per_span(self, resource_version_id, fingerprint):
        spans = [
            SilenceSpanData(start_ms=1000, end_ms=2000),
            SilenceSpanData(start_ms=5000, end_ms=6000),
        ]
        provenance = ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="audio_silence_detect_ffmpeg",
            adapter_version="1.0.0",
        )

        representations = build_silence_span_representations(
            spans,
            resource_version_id,
            fingerprint,
            provenance,
            track_duration_ms=10_000,
        )

        assert len(representations) == 2
        first = representations[0]
        assert first.kind == MediaRepresentationKind.SILENCE_SPAN
        assert first.locators[0].locator_type == "time_range"
        assert (first.locators[0].start_ms, first.locators[0].end_ms) == (1000, 2000)
        # Silence carries no text: it is positionally useful, not retrievable.
        assert first.textual_payload == ""

    def test_expander_coverage_is_silence_over_track_duration(self, resource_version_id, fingerprint):
        spans = [SilenceSpanData(start_ms=0, end_ms=2000)]
        provenance = ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="audio_silence_detect_ffmpeg",
            adapter_version="1.0.0",
        )

        representations = build_silence_span_representations(
            spans, resource_version_id, fingerprint, provenance, track_duration_ms=10_000
        )

        assert representations[0].coverage.coverage_fraction == pytest.approx(0.2)

    def test_expander_with_no_spans_produces_no_representations(self, resource_version_id, fingerprint):
        provenance = ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="audio_silence_detect_ffmpeg",
            adapter_version="1.0.0",
        )

        # A track with no silence is a valid result, not a failure. There is
        # no representation to carry a 0.0 coverage, and that is correct:
        # coverage describes a representation that exists.
        representations = build_silence_span_representations(
            [], resource_version_id, fingerprint, provenance, track_duration_ms=10_000
        )

        assert representations == []

    def test_definition_carries_threshold_and_min_duration(self):
        definition = build_silence_detect_definition(
            noise_threshold_db=-40.0, min_silence_ms=500
        )

        joined = " ".join(definition.fixed_args)
        assert "noise=-40.0dB" in joined
        assert "d=0.5" in joined

    def test_definition_is_deterministic_and_offline(self):
        definition = build_silence_detect_definition()

        assert definition.producer_type == MediaProducerType.DETERMINISTIC
        assert definition.network_disabled is True
        assert definition.stage == PipelineStage.DETECT_SILENCE
```

Imports these tests need. From `katsi_core.media.audio_pipeline`: `AudioSilenceDetectionPipeline`, `build_silence_detect_definition`, `build_silence_span_representations`, `SilenceSpanData`. From `katsi_core.media.contracts`: `DerivedRepresentation`, `ProducerProvenance`, `MediaProducerType`, `PipelineStage`. Several may already be present — add only what is missing. `json` and `sys` are already imported at the top of this test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_media_audio_pipeline.py::TestAudioSilenceDetectionPipeline -v`
Expected: FAIL — `ImportError: cannot import name 'build_silence_detect_definition'`

- [ ] **Step 3: Add the enum members**

In `packages/core/katsi_core/media/contracts.py`, add to `MediaRepresentationKind` after `SCENE = "scene"` (`:56`):

```python
    SILENCE_SPAN = "silence_span"
```

And to `PipelineStage` after `SEGMENT_SPEAKERS = "segment_speakers"` (`:119`):

```python
    DETECT_SILENCE = "detect_silence"
```

- [ ] **Step 4: Add the definition builder**

In `packages/core/katsi_core/media/audio_pipeline.py`, after `parse_silence_spans`:

```python
def build_silence_detect_definition(
    *,
    executable_path: str = "ffmpeg",
    noise_threshold_db: float = -35.0,
    min_silence_ms: int = 250,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 20_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for measuring silence with ffmpeg.

    ``noise_threshold_db`` and ``min_silence_ms`` are parameters rather than
    constants because a real recording needs them tuned per microphone and
    room; the defaults suit typical spoken-word capture.

    Consumes the normalized WAV proxy that the decode stage already produced,
    so this never decodes anything itself. Findings arrive on stderr, not in
    an output file.
    """
    return MediaPipelineDefinition(
        id="audio_silence_detect_ffmpeg_v1",
        name="Measure silence spans",
        description="Deterministic silence measurement over a normalized audio proxy.",
        stage=PipelineStage.DETECT_SILENCE,
        accepted_mime_patterns=["audio/*"],
        input_kinds=[MediaRepresentationKind.PROXY_MEDIA],
        representation_kinds_produced=[MediaRepresentationKind.SILENCE_SPAN],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-i",
            "{input_path}",
            "-af",
            f"silencedetect=noise={noise_threshold_db}dB:d={min_silence_ms / 1000}",
            "-f",
            "null",
            "-",
        ],
        allowed_env_vars=[],
        working_directory=".",
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )
```

- [ ] **Step 5: Add the pipeline class**

Immediately after the builder:

```python
class AudioSilenceDetectionPipeline(MediaPipelineProtocol):
    """Deterministic silence measurement producing one representation per span.

    Kept separate from the transcription segment stream on purpose: ffmpeg is
    :attr:`MediaProducerType.DETERMINISTIC` while transcription is
    ``MODEL_BACKED``, and merging them would make a measured boundary
    indistinguishable from an inferred one.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_silence_detect_definition()
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "audio_silence_detect_ffmpeg"

    @classmethod
    def get_adapter_version(cls) -> str:
        return "1.0.0"

    def get_pipeline_definition(self) -> MediaPipelineDefinition:  # type: ignore[override]
        return self._definition

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.FFMPEG]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
        *,
        track_duration_ms: int,
    ) -> DerivedRepresentation:
        if track_duration_ms <= 0:
            raise ValueError("track_duration_ms must be positive")

        result = self._executor.execute(self._definition, file_path, working_directory)

        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Silence detection failed: exit_code={result.exit_code} "
                f"timed_out={result.timed_out} stderr={result.stderr_sample[:500]!r}"
            )
        # A truncated stderr silently loses real spans; never parse it.
        if result.output_truncated:
            raise RuntimeError("Silence detection output was truncated; refusing to parse")

        spans = parse_silence_spans(result.stderr_sample, track_duration_ms=track_duration_ms)
        batch = json.dumps(
            {
                "track_duration_ms": track_duration_ms,
                "spans": [{"start_ms": s.start_ms, "end_ms": s.end_ms} for s in spans],
            }
        )

        total_silent_ms = sum(span.end_ms - span.start_ms for span in spans)
        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.SILENCE_SPAN,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=batch,
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(
                is_complete=True,
                coverage_fraction=min(total_silent_ms / track_duration_ms, 1.0),
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name=self.get_adapter_name(),
                adapter_version=self.get_adapter_version(),
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Expected a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.SILENCE_SPAN:
            return False, "Expected a SILENCE_SPAN representation"
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful detection"
        return True, None
```

- [ ] **Step 6: Add the expander**

Immediately after the class, mirroring `build_segment_representations` (`:669`):

```python
def build_silence_span_representations(
    spans: list[SilenceSpanData],
    resource_version_id: ResourceVersionId,
    pipeline_fingerprint: PipelineFingerprint,
    adapter: ProducerProvenance,
    *,
    track_duration_ms: int,
) -> list[DerivedRepresentation]:
    """Expand measured spans into N per-span representations.

    Each span becomes its own immutable representation carrying exactly one
    :class:`TimeRangeLocator`, so a consumer always cites a single,
    unambiguous time range. Silence carries no ``textual_payload``: it is
    positionally useful, not semantically retrievable, and never receives an
    embedding.

    A track with no silence yields no representations. That is a valid
    result, not a failure.
    """
    if track_duration_ms <= 0:
        raise ValueError("track_duration_ms must be positive")

    total_silent_ms = sum(span.end_ms - span.start_ms for span in spans)
    coverage_fraction = min(total_silent_ms / track_duration_ms, 1.0)

    now = datetime.now(UTC)
    representations: list[DerivedRepresentation] = []
    for span in spans:
        rep_id = uuid4()
        representations.append(
            DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.SILENCE_SPAN,
                media_type="application/json",
                status=MediaRepresentationStatus.CURRENT,
                created_at=now,
                updated_at=now,
                textual_payload="",
                locators=(
                    TimeRangeLocator(
                        resource_version_id=resource_version_id,
                        representation_id=rep_id,
                        start_ms=span.start_ms,
                        end_ms=span.end_ms,
                    ),
                ),
                coverage=MediaCoverage(
                    is_complete=False, coverage_fraction=coverage_fraction
                ),
                producer=adapter,
                pipeline_fingerprint=pipeline_fingerprint,
            )
        )

    return representations
```

`TimeRangeLocator` and `WholeResourceLocator` are already imported in this module. If `json` is not already imported at module level, add it.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_media_audio_pipeline.py::TestAudioSilenceDetectionPipeline -v`
Expected: PASS

- [ ] **Step 8: Confirm the feature stays gated**

Run: `grep -n "enable_audio_processing" packages/core/katsi_core/media/contracts.py`
Expected: still `default=False`.

Run: `grep -rn "AudioSilenceDetectionPipeline" packages/core/katsi_core/media/pipeline_registry.py`
Expected: no matches — the pipeline must not be auto-registered.

- [ ] **Step 9: Run the full media suite for regressions**

Run: `pytest tests/ -k media -v`
Expected: PASS. New enum members are additive; nothing should break.

- [ ] **Step 10: Commit**

```bash
git add packages/core/katsi_core/media/contracts.py packages/core/katsi_core/media/audio_pipeline.py tests/test_media_audio_pipeline.py
git commit -m "feat: add deterministic silence detection pipeline

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Project silence spans into the graph

**Files:**
- Modify: `packages/core/katsi_core/store/graph.py` (add node and rel tables near `:74` and `:92`; add a branch to `_project_media_locator` at `:459`)
- Test: `tests/test_media_vector_projection.py` (this file already holds the media graph-projection test, `test_graph_projection_removes_noncurrent_visibility` at `:120`, and the `_representation` helper)

**Interfaces:**
- Consumes: `MediaRepresentationKind.SILENCE_SPAN` from Task 3.
- Produces: a `SilenceSpan(id STRING, start_ms INT64, end_ms INT64)` node table and a `HAS_SILENCE_SPAN` relation from `MediaResourceVersion`.

**Background the implementer needs — read this before editing:** `_project_media_locator` (`:459`) dispatches on `locator_type` first, then falls through to an `elif` on `item.kind` for transcript segments. Transcript segment representations also carry `time_range` locators. So adding an `elif locator_type == "time_range"` branch **would silently steal transcript segments away from `HAS_TRANSCRIPT_SEGMENT`**. Dispatch on `item.kind is MediaRepresentationKind.SILENCE_SPAN` instead, and place it *before* the transcript branch. Silence spans never carry page, scene, or video_frame locators, so the earlier branches cannot catch them.

Two API facts to get right, both easy to assume wrong:

- `GraphStore.project_media_representations` (`:424`) takes **only** `representations: list[DerivedRepresentation]`. There is no `resource_id` parameter — the resource comes from each item's `resource_version_id`.
- `pipeline_fingerprint` is a structured `PipelineFingerprint` object, never a string. Build it with the keyword constructor, as `_representation` does at `tests/test_media_vector_projection.py:51`.

Tests in this file query the graph directly through `graph._conn.execute(...).get_next()`. Follow that; do not add a public query helper for the test's convenience.

Unlike `MediaScene` and `TranscriptSegment`, which are id-only, this node stores its times — following the `MediaPage` precedent (`:67`), which stores `number`. A consumer needs the milliseconds without a second lookup.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_vector_projection.py`:

```python
def _silence_representation(*, start_ms: int = 1000, end_ms: int = 2000):
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.SILENCE_SPAN,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="",
        locators=(
            TimeRangeLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.1),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="audio_silence_detect_ffmpeg",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.SILENCE_SPAN,
            stage=PipelineStage.DETECT_SILENCE,
            adapter_name="audio_silence_detect_ffmpeg",
            adapter_version="1.0.0",
            sampling_fingerprint="b" * 64,
        ),
    )


def _transcript_representation():
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="hello",
        locators=(
            TimeRangeLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                start_ms=0,
                end_ms=1000,
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="c" * 64,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=PipelineStage.TRANSCRIBE,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
            sampling_fingerprint="d" * 64,
        ),
    )


def test_silence_span_projects_with_its_times(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _silence_representation(start_ms=1000, end_ms=2000)

    graph.project_media_representations([representation])

    row = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_SILENCE_SPAN]->(s:SilenceSpan) "
        "RETURN s.start_ms, s.end_ms",
        {"id": str(representation.resource_version_id)},
    ).get_next()
    assert row == [1000, 2000]


def test_transcript_segment_still_projects_alongside_silence(tmp_path):
    """Regression guard: silence dispatch must not steal time_range locators."""
    graph = GraphStore(tmp_path / "graph")
    transcript = _transcript_representation()

    graph.project_media_representations([transcript, _silence_representation()])

    count = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_TRANSCRIPT_SEGMENT]->(t:TranscriptSegment) "
        "RETURN count(t)",
        {"id": str(transcript.resource_version_id)},
    ).get_next()[0]
    assert count == 1
```

Add `TimeRangeLocator` to this file's `katsi_core.media.contracts` import block. `DerivedRepresentation`, `MediaCoverage`, `MediaProducerType`, `MediaRepresentationKind`, `MediaRepresentationStatus`, `PipelineFingerprint`, `PipelineStage`, and `ProducerProvenance` are already imported there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_media_vector_projection.py -k silence -v`
Expected: FAIL — the `SilenceSpan` table does not exist.

- [ ] **Step 3: Add the schema**

In `packages/core/katsi_core/store/graph.py`, after the `TranscriptSegment` node table (`:74`):

```python
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS SilenceSpan("
            "id STRING, start_ms INT64, end_ms INT64, PRIMARY KEY(id))"
        )
```

And after the `HAS_TRANSCRIPT_SEGMENT` rel table (`:92`):

```python
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS HAS_SILENCE_SPAN(FROM MediaResourceVersion TO SilenceSpan)"
        )
```

- [ ] **Step 4: Add the projection branch**

In `_project_media_locator`, insert this branch **immediately before** the existing `elif item.kind is MediaRepresentationKind.TRANSCRIPT_SEGMENT:` at `:474`:

```python
            elif item.kind is MediaRepresentationKind.SILENCE_SPAN:
                self._conn.execute(
                    "MERGE (s:SilenceSpan {id: $id}) "
                    "SET s.start_ms = $start_ms, s.end_ms = $end_ms",
                    {
                        "id": str(item.id),
                        "start_ms": locator_data["start_ms"],
                        "end_ms": locator_data["end_ms"],
                    },
                )
                self._connect_media_node(
                    resource_id, "SilenceSpan", str(item.id), "HAS_SILENCE_SPAN"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_media_vector_projection.py -v`
Expected: PASS, both the new projection test and the transcript regression guard.

- [ ] **Step 6: Run the full suite**

Run: `pytest tests/ -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add packages/core/katsi_core/store/graph.py tests/test_media_vector_projection.py
git commit -m "feat: project silence spans into the media graph

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification

After all four tasks, confirm the boundary held:

- [ ] `grep -rin "tempo\|\bbeat\b\|librosa\|aubio\|loudness\|ebur128" packages/core/katsi_core/` returns no matches. Edit-time analysis must not have leaked into katsi.
- [ ] `grep -n "enable_audio_processing" packages/core/katsi_core/media/contracts.py` still shows `default=False`.
- [ ] `pytest tests/ -v` passes.
