# Katsi audio precision: word-level timing and measured silence

**Date:** 2026-08-21
**Status:** Approved design, not yet implemented
**Scope:** katsi core media pipeline only

## Problem

Katsi can already say *which* audio or video file contains a spoken phrase, and
roughly when: `TranscriptSegment` nodes carry `start_ms`/`end_ms` at whisper's
segment granularity, typically several seconds wide. `TimeRangeLocator` and
`VideoFrameLocator` can address any instant, but nothing produces timings finer
than a segment.

That resolution is too coarse for a consumer that needs to cut footage. A caller
asking "where does she say *the second one*" gets a five-second window and has to
guess the boundary inside it. Separately, katsi models silence as a segment kind
(`_ALLOWED_SEGMENT_KINDS` in `media/audio_pipeline.py:445`) but never measures
it — a `silence` segment only exists if the configured transcription tool
volunteers one.

Both gaps are about the precision of an answer katsi already gives. Neither
requires katsi to learn anything about editing.

## Boundary

This design deliberately stops at *where and when*. Katsi does not compute beat
positions, energy envelopes, cut points, or edit decision lists. A consumer that
needs those runs its own analysis on the file katsi located, reached through the
existing `open_media_original` tool.

The line: a property measured *of* a segment belongs in katsi; a decision made
*about* a segment belongs in the consumer. Silence is a measurable property of
the waveform. Tempo only matters once something is being cut, so it lives
downstream.

## Design

### 1. Word-level timings on speech segments

Whisper produces per-word timings when asked. Katsi's transcription contract does
not ask, and its parser would discard them if it did.

Note that `build_transcribe_definition` (`media/audio_pipeline.py:461`) does not
invoke the stock whisper CLI directly — it specifies a strict JSON contract
requiring `segments` and `coverage_fraction`, keys the stock CLI does not emit.
The configured executable is an owner-supplied wrapper. So this change extends
the contract that wrapper must satisfy, and the wrapper is what passes
`--word_timestamps True` to whisper underneath.

**New type**, alongside `TranscriptSegmentData`:

```python
@dataclass(frozen=True, slots=True)
class WordTimingData:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None
```

**`TranscriptSegmentData`** gains `words: tuple[WordTimingData, ...] = ()`.

**`parse_transcript_segments`** (`media/audio_pipeline.py:607`) parses an
optional `words` array per segment, applying the same strictness the existing
parser applies to segments:

- `end_ms` must exceed `start_ms` for every word.
- Every word range must fall within its parent segment's range. A word outside
  its segment is a contract violation, not something to clamp.
- Words must be time-ordered and non-overlapping.
- `confidence`, when present, must be within `[0.0, 1.0]`.
- A non-speech segment must not carry words, mirroring the existing rule at
  `:638` that non-speech segments must not carry text.

The array is optional so that transcription wrappers already deployed against the
current contract keep working; they simply produce segments with no words.

**Consumption.** Word timings ride along inside the transcript segment
representation. They do not become their own representation, their own graph
node, or their own embedding — a word is not independently meaningful to
retrieve. A caller that has located a segment can narrow to a phrase within it
using the word array and construct a `TimeRangeLocator` at that precision.

### 2. Measured silence spans

Silence gets its own pipeline rather than being folded into the transcript
timeline, because the two have different provenance. Whisper is
`MediaProducerType.MODEL_BACKED`; ffmpeg's `silencedetect` filter is
`DETERMINISTIC`. Merging them into one segment stream would make a measured
boundary indistinguishable from an inferred one, which is exactly the distinction
katsi's provenance model exists to preserve.

**New enum members:**

- `PipelineStage.DETECT_SILENCE = "detect_silence"`
- `MediaRepresentationKind.SILENCE_SPAN = "silence_span"`

**New pipeline** `AudioSilenceDetectionPipeline`, following the shape of
`AudioDecodePipeline` (`media/audio_pipeline.py:340`):

- Consumes `MediaRepresentationKind.PROXY_MEDIA` — the normalized WAV the decode
  stage already produces, so no new decode work.
- Runs ffmpeg with `silencedetect` through the existing
  `BoundedSubprocessExecutor`, under an owner-authored
  `build_silence_detect_definition` with `network_disabled=True`, matching every
  other definition in the module.
- Declares `SoftwareDependency.FFMPEG` and `HardwareRequirement.NONE`.
- Parses ffmpeg's `silence_start` / `silence_end` stderr lines into
  `SilenceSpanData(start_ms, end_ms)`, and produces one representation per span
  carrying a `TimeRangeLocator`.
- Threshold and minimum duration are parameters on the definition builder with
  documented defaults (`-35dB`, `250ms`), not constants. These are the knobs a
  real recording needs tuned per microphone and room.

**Coverage.** The representation set reports `MediaCoverage` computed from total
silence duration over track duration, consistent with how transcription reports
`coverage_fraction`.

**Graph.** Silence spans attach to the resource version through a new
`HAS_SILENCE_SPAN` relation to a `SilenceSpan` node holding `start_ms`/`end_ms`.
They carry no text and receive no embedding — silence is not semantically
retrievable, only positionally useful.

### 3. Video

No video-specific work. `VideoAudioExtractionPipeline`
(`media/video_pipeline.py:693`) already extracts the track to a WAV proxy and
routes it through the real audio pipelines, so both features reach video for free
once the pipelines are registered.

## Configuration

`MediaProcessingConfig.enable_audio_processing` (`media/contracts.py:734`) stays
`False` by default, and the new pipeline stays out of the default registry in
`media/pipeline_registry.py`, consistent with every existing audio pipeline. A
workspace that wants this registers it explicitly. Existing katsi workspaces see
no behavior change.

## Testing

Word timings, against `parse_transcript_segments`:

- A segment with a valid `words` array parses, preserving order and count.
- A segment with no `words` key parses with `words == ()`.
- A word whose range escapes its parent segment raises `ValueError`.
- Overlapping or out-of-order words raise `ValueError`.
- A `silence` or `music` segment carrying words raises `ValueError`.
- A word with `confidence` outside `[0.0, 1.0]` raises `ValueError`.

Silence detection:

- A parser test over captured ffmpeg `silencedetect` stderr, including the
  trailing-silence case where ffmpeg emits `silence_start` with no matching
  `silence_end` before EOF. That span must close at track duration rather than
  being dropped.
- A test that a track with no silence produces zero spans and coverage `0.0`,
  rather than failing.
- Threshold and minimum-duration parameters reach the ffmpeg argument list.

Both follow the existing strictness convention in this module: invalid input
raises rather than being silently repaired.

## Out of scope

Beat and tempo detection, loudness envelopes, audio event and SFX
classification, audio embeddings (CLAP or otherwise), speaker identity
resolution, and edit decision lists. The first four are consumer-side editing
concerns under the boundary above. Speaker identity is separately gated by
`AudioSpeakerSegmentationPipeline`'s deliberate anonymous-label restriction
(`media/audio_pipeline.py:1015`), which this design does not revisit.

One observation, not addressed here: `parse_transcript_segments` constructs
`TranscriptSegmentData` without populating `speaker_label`, though the field
exists. Speaker labels are merged into segment text later by the diarization path
(`media/audio_pipeline.py:698`). Whether the field should be populated at parse
time is a separate question from this design.

## Follow-on specs

1. `asset-bridge` — the BrandOss consumer that queries katsi over MCP and emits
   the `inicio_s`/`dur_s`/`shot_id` EDL contract, including its own beat and
   energy analysis on located footage.
2. Visual region extraction — populating `ImageRegionLocator` bounding boxes for
   video keyframes, so "where in the frame" is answerable alongside "when".
