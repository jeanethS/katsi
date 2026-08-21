"""Tests for Section 7 (Audio Understanding) of multimedia-understanding.

Covers tasks 7.1-7.7: deterministic audio metadata extraction, bounded local
decoding, local speech transcription with strict segments/coverage,
transcript chunk assembly without duplicate evidence, anonymous speaker
segmentation, silence/music/unrecognized representation, and fixtures for
mono/stereo, multiple speakers, silence, partial duration, decoder failure,
and cache reuse.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import ChunkingThresholds, MediaSamplingSettings, SQLiteSettings
from katsi_core.media.audio_pipeline import (
    SPEAKER_LABEL_PATTERN,
    AudioDecodePipeline,
    AudioMetadataError,
    AudioMetadataPipeline,
    AudioSpeakerSegmentationPipeline,
    AudioTranscriptionPipeline,
    TranscriptSegmentData,
    WordTimingData,
    apply_speaker_labels,
    assemble_transcript_chunks,
    build_decode_definition,
    build_segment_representations,
    build_transcribe_definition,
    parse_speaker_segments,
    parse_transcript_segments,
    parse_wav_metadata,
)
from katsi_core.media.cache import RepresentationCache
from katsi_core.media.contracts import (
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    ProducerProvenance,
)
from katsi_core.media.execution import PipelineExecutionOrchestrator
from katsi_core.media.fingerprint import build_pipeline_fingerprint
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.store.workspace_sqlite import WorkspaceSQLite

# ---------------------------------------------------------------------------
# Synthetic WAV fixture builder
# ---------------------------------------------------------------------------


def _build_wav_bytes(
    *,
    channels: int = 1,
    sample_rate: int = 8000,
    bits_per_sample: int = 16,
    num_frames: int = 800,
    truncate_data_bytes: int | None = None,
) -> bytes:
    """Build a minimal, valid PCM WAV file with silent (zeroed) samples."""
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align
    data = b"\x00" * (num_frames * block_align)
    if truncate_data_bytes is not None:
        declared_data_size = len(data)
        data = data[:truncate_data_bytes]
    else:
        declared_data_size = len(data)

    fmt_chunk = struct.pack(
        "<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits_per_sample
    )
    chunks = b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    chunks += b"data" + struct.pack("<I", declared_data_size) + data
    riff_size = 4 + len(chunks)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + chunks


@pytest.fixture
def resource_version_id():
    return uuid4()


@pytest.fixture
def source_content_hash():
    return "a" * 32


# ---------------------------------------------------------------------------
# 7.1 Deterministic audio metadata extraction
# ---------------------------------------------------------------------------


class TestAudioMetadataExtraction:
    def test_mono_wav_metadata(self):
        wav = _build_wav_bytes(channels=1, sample_rate=8000, num_frames=8000)
        info = parse_wav_metadata(wav)

        assert info.container == "wav"
        assert info.codec == "pcm"
        assert info.channels == 1
        assert info.sample_rate == 8000
        assert info.duration_ms == 1000

    def test_stereo_wav_metadata(self):
        wav = _build_wav_bytes(channels=2, sample_rate=44100, num_frames=44100)
        info = parse_wav_metadata(wav)

        assert info.channels == 2
        assert info.sample_rate == 44100
        assert info.duration_ms == 1000

    def test_partial_duration_truncated_data_chunk(self):
        # Declared data size implies 1000ms, but only half the bytes are
        # actually present (interrupted capture). Duration must reflect the
        # bytes actually present, not the declared/fabricated size.
        wav = _build_wav_bytes(
            channels=1, sample_rate=8000, num_frames=8000, truncate_data_bytes=8000
        )
        info = parse_wav_metadata(wav)

        assert info.duration_ms == 500

    def test_malformed_container_raises(self):
        with pytest.raises(AudioMetadataError):
            parse_wav_metadata(b"not a wav file at all")

    def test_missing_fmt_chunk_raises(self):
        data = b"\x00" * 40
        chunks = b"data" + struct.pack("<I", len(data)) + data
        wav = b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
        with pytest.raises(AudioMetadataError):
            parse_wav_metadata(wav)

    def test_metadata_pipeline_via_orchestrator(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        wav_path = tmp_path / "sample.wav"
        wav_path.write_bytes(_build_wav_bytes(channels=1, sample_rate=16000, num_frames=16000))

        adapter = AudioMetadataPipeline()
        definition = adapter.get_pipeline_definition()
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.METADATA,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, wav_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.CURRENT
        payload = json.loads(representation.textual_payload)
        assert payload["channels"] == 1
        assert payload["sample_rate"] == 16000
        assert payload["duration_ms"] == 1000

    def test_decoder_failure_produces_failed_representation(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        # Malformed input triggers the adapter's own AudioMetadataError; the
        # orchestrator must convert this into a structured FAILED
        # representation after exhausting retries, not propagate a raw
        # exception.
        bad_path = tmp_path / "bad.wav"
        bad_path.write_bytes(b"definitely not a wav file")

        adapter = AudioMetadataPipeline()
        definition = adapter.get_pipeline_definition()
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.METADATA,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, bad_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.FAILED
        assert representation.error is not None
        assert not representation.coverage.is_complete


# ---------------------------------------------------------------------------
# 7.2 Bounded local decoding
# ---------------------------------------------------------------------------


class TestAudioDecodePipeline:
    def _fake_decode_definition(self, tmp_path):
        # A safe stand-in for ffmpeg: a python3 script that copies input
        # bytes to output_path, proving the decode adapter only ever routes
        # through BoundedSubprocessExecutor with a fixed template -- never a
        # raw subprocess call of its own.
        script = tmp_path / "fake_ffmpeg.py"
        script.write_text(
            "import shutil, sys\nsrc, dst = sys.argv[1], sys.argv[2]\nshutil.copyfile(src, dst)\n"
        )
        return build_decode_definition(executable_path=sys.executable).model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": [str(script), "{input_path}", "{output_path}"],
            }
        )

    def test_decode_produces_proxy_representation(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())

        definition = self._fake_decode_definition(tmp_path)
        adapter = AudioDecodePipeline(definition)
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.PROXY_MEDIA,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, input_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.CURRENT
        assert representation.kind == MediaRepresentationKind.PROXY_MEDIA
        assert representation.blob_hash is not None
        assert representation.blob_byte_count == len(_build_wav_bytes())

    def test_decoder_failure_produces_failed_representation(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())

        # A script that always exits non-zero, simulating a decoder failure.
        failing_script = tmp_path / "failing.py"
        failing_script.write_text("import sys\nsys.exit(1)\n")
        definition = build_decode_definition().model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": [str(failing_script)],
                "retry_on_failure": False,
            }
        )
        adapter = AudioDecodePipeline(definition)
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.PROXY_MEDIA,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, input_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.FAILED
        assert representation.error is not None


# ---------------------------------------------------------------------------
# 7.3 / 7.6 Transcription: strict segments, coverage, no fabrication
# ---------------------------------------------------------------------------


class TestTranscriptSegmentParsing:
    def test_parses_speech_silence_music_unrecognized_segments(self):
        batch = {
            "coverage_fraction": 0.75,
            "segments": [
                {"start_ms": 0, "end_ms": 1000, "segment_kind": "silence", "text": ""},
                {
                    "start_ms": 1000,
                    "end_ms": 3000,
                    "segment_kind": "speech",
                    "text": "hello world",
                    "confidence": 0.92,
                },
                {"start_ms": 3000, "end_ms": 4000, "segment_kind": "music", "text": ""},
                {
                    "start_ms": 4000,
                    "end_ms": 5000,
                    "segment_kind": "unrecognized",
                    "text": "",
                },
                {
                    "start_ms": 5000,
                    "end_ms": 6000,
                    "segment_kind": "unsupported_language",
                    "text": "",
                    "language": "xx",
                },
            ],
        }

        segments, coverage_fraction = parse_transcript_segments(batch)

        assert coverage_fraction == 0.75
        assert [s.segment_kind for s in segments] == [
            "silence",
            "speech",
            "music",
            "unrecognized",
            "unsupported_language",
        ]
        assert segments[1].text == "hello world"
        # Non-speech segments never carry fabricated text.
        assert all(s.text == "" for s in segments if s.segment_kind != "speech")

    def test_non_speech_segment_with_text_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [{"start_ms": 0, "end_ms": 100, "segment_kind": "silence", "text": "oops"}],
        }
        with pytest.raises(ValueError, match="must not carry text"):
            parse_transcript_segments(batch)

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
        assert isinstance(segments[0].words[0], WordTimingData)
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

    def test_invalid_time_range_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [{"start_ms": 100, "end_ms": 100, "segment_kind": "speech", "text": "x"}],
        }
        with pytest.raises(ValueError):
            parse_transcript_segments(batch)

    def test_unknown_segment_kind_rejected(self):
        batch = {
            "coverage_fraction": 1.0,
            "segments": [{"start_ms": 0, "end_ms": 100, "segment_kind": "laughter", "text": ""}],
        }
        with pytest.raises(ValueError, match="Unknown segment_kind"):
            parse_transcript_segments(batch)


class TestAudioTranscriptionPipeline:
    def _fake_transcribe_definition(self, tmp_path, batch: dict):
        # fixed_args go through str.format() substitution (execution.py
        # ALLOWED_ARG_PLACEHOLDERS), so a literal JSON payload containing
        # `{`/`}` cannot be embedded directly as an arg -- write it to a
        # script file instead and invoke that file with no braces in argv.
        script = tmp_path / "fake_whisper.py"
        script.write_text(f"print({json.dumps(json.dumps(batch))})\n")
        return build_transcribe_definition().model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": [str(script)],
            }
        )

    def test_transcription_pipeline_via_orchestrator(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())

        batch = {
            "coverage_fraction": 1.0,
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "segment_kind": "speech",
                    "text": "hi there",
                    "confidence": 0.9,
                }
            ],
        }
        definition = self._fake_transcribe_definition(tmp_path, batch)
        adapter = AudioTranscriptionPipeline(definition)
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, input_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.CURRENT
        parsed = json.loads(representation.textual_payload)
        assert parsed["segments"][0]["text"] == "hi there"

    def test_invalid_json_output_produces_failed_representation(
        self, resource_version_id, source_content_hash, tmp_path
    ):
        input_path = tmp_path / "in.wav"
        input_path.write_bytes(_build_wav_bytes())

        definition = build_transcribe_definition().model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": ["-c", "print('not json at all')"],
                "retry_on_failure": False,
            }
        )
        adapter = AudioTranscriptionPipeline(definition)
        settings = MediaSamplingSettings()
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=definition.stage,
            adapter_name=adapter.get_adapter_name(),
            adapter_version=adapter.get_adapter_version(),
            settings=settings,
        )

        orchestrator = PipelineExecutionOrchestrator()
        representation = orchestrator.run(
            adapter, definition, input_path, resource_version_id, source_content_hash, fingerprint
        )

        assert representation.status == MediaRepresentationStatus.FAILED


# ---------------------------------------------------------------------------
# 7.4 Transcript chunk assembly
# ---------------------------------------------------------------------------


class TestTranscriptChunkAssembly:
    def _adapter(self):
        return ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
        )

    def _fingerprint(self, resource_version_id, source_content_hash):
        settings = MediaSamplingSettings()
        return build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=__import__(
                "katsi_core.media.contracts", fromlist=["PipelineStage"]
            ).PipelineStage.TRANSCRIBE,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
            settings=settings,
        )

    def test_chunk_assembly_preserves_locators_no_duplicates(
        self, resource_version_id, source_content_hash
    ):
        fingerprint = self._fingerprint(resource_version_id, source_content_hash)
        segments = [
            TranscriptSegmentData(
                start_ms=i * 1000,
                end_ms=(i + 1) * 1000,
                text=f"word{i}",
                confidence=0.9,
                segment_kind="speech",
            )
            for i in range(5)
        ]
        reps = build_segment_representations(
            segments, resource_version_id, fingerprint, self._adapter()
        )

        chunks = assemble_transcript_chunks(reps, settings=ChunkingThresholds(target_tokens=64))

        # All source time ranges must be represented across chunks exactly once.
        all_locators = [loc for chunk in chunks for loc in chunk.locators]
        assert len(all_locators) == len(reps)
        ranges = [(loc.start_ms, loc.end_ms) for loc in all_locators]
        assert len(ranges) == len(set(ranges))  # no duplicate evidence

        # Text is preserved.
        combined_text = " ".join(chunk.textual_payload for chunk in chunks)
        for i in range(5):
            assert f"word{i}" in combined_text

    def test_non_speech_segments_never_merged_into_chunks(
        self, resource_version_id, source_content_hash
    ):
        fingerprint = self._fingerprint(resource_version_id, source_content_hash)
        segments = [
            TranscriptSegmentData(
                start_ms=0, end_ms=1000, text="", confidence=None, segment_kind="silence"
            ),
            TranscriptSegmentData(
                start_ms=1000, end_ms=2000, text="hello", confidence=0.9, segment_kind="speech"
            ),
        ]
        reps = build_segment_representations(
            segments, resource_version_id, fingerprint, self._adapter()
        )

        chunks = assemble_transcript_chunks(reps)

        assert len(chunks) == 1
        assert chunks[0].textual_payload == "hello"

    def test_small_target_forces_multiple_chunks(self, resource_version_id, source_content_hash):
        fingerprint = self._fingerprint(resource_version_id, source_content_hash)
        segments = [
            TranscriptSegmentData(
                start_ms=i * 1000,
                end_ms=(i + 1) * 1000,
                text="a much longer piece of transcript text here that pushes past the target token budget",
                confidence=0.9,
                segment_kind="speech",
            )
            for i in range(4)
        ]
        reps = build_segment_representations(
            segments, resource_version_id, fingerprint, self._adapter()
        )

        chunks = assemble_transcript_chunks(reps, settings=ChunkingThresholds(target_tokens=64))

        assert len(chunks) > 1
        total_locators = sum(len(c.locators) for c in chunks)
        assert total_locators == len(reps)


# ---------------------------------------------------------------------------
# 7.5 Anonymous speaker segmentation
# ---------------------------------------------------------------------------


class TestSpeakerSegmentation:
    def test_valid_anonymous_labels_accepted(self):
        payload = {
            "speaker_segments": [
                {"start_ms": 0, "end_ms": 1000, "speaker_label": "SPEAKER_1"},
                {"start_ms": 1000, "end_ms": 2000, "speaker_label": "SPEAKER_2"},
            ]
        }
        segments = parse_speaker_segments(payload)
        assert [s.speaker_label for s in segments] == ["SPEAKER_1", "SPEAKER_2"]

    def test_real_name_label_rejected(self):
        payload = {
            "speaker_segments": [{"start_ms": 0, "end_ms": 1000, "speaker_label": "John Smith"}]
        }
        with pytest.raises(ValueError, match="non-anonymous"):
            parse_speaker_segments(payload)

    def test_speaker_label_pattern_rejects_non_numeric_suffix(self):
        assert SPEAKER_LABEL_PATTERN.match("SPEAKER_1")
        assert not SPEAKER_LABEL_PATTERN.match("SPEAKER_A")
        assert not SPEAKER_LABEL_PATTERN.match("Jane Doe")

    def test_apply_speaker_labels_by_overlap(self):
        transcript_segments = [
            TranscriptSegmentData(0, 1000, "hi", 0.9, "speech"),
            TranscriptSegmentData(1000, 2000, "there", 0.9, "speech"),
            TranscriptSegmentData(5000, 6000, "unmatched", 0.9, "speech"),
        ]
        speaker_segments = parse_speaker_segments(
            {
                "speaker_segments": [
                    {"start_ms": 0, "end_ms": 1000, "speaker_label": "SPEAKER_1"},
                    {"start_ms": 1000, "end_ms": 2000, "speaker_label": "SPEAKER_2"},
                ]
            }
        )

        labeled = apply_speaker_labels(transcript_segments, speaker_segments)

        assert labeled[0].speaker_label == "SPEAKER_1"
        assert labeled[1].speaker_label == "SPEAKER_2"
        assert labeled[2].speaker_label is None  # never guess

    def test_validate_output_rejects_non_anonymous_label(self):
        adapter = AudioSpeakerSegmentationPipeline()
        from datetime import UTC, datetime
        from uuid import uuid4 as _uuid4

        from katsi_core.media.contracts import (
            DerivedRepresentation,
            MediaCoverage,
            PipelineFingerprint,
            PipelineStage,
            WholeResourceLocator,
        )

        rvid = _uuid4()
        rep_id = _uuid4()
        bad_payload = json.dumps(
            {"speaker_segments": [{"start_ms": 0, "end_ms": 1000, "speaker_label": "Real Name"}]}
        )
        rep = DerivedRepresentation(
            id=rep_id,
            resource_version_id=rvid,
            kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            textual_payload=bad_payload,
            locators=(WholeResourceLocator(resource_version_id=rvid, representation_id=rep_id),),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
                adapter_name="audio_speaker_segmentation",
                adapter_version="1.0.0",
            ),
            pipeline_fingerprint=PipelineFingerprint(
                source_content_hash="a" * 32,
                representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
                stage=PipelineStage.SEGMENT_SPEAKERS,
                adapter_name="audio_speaker_segmentation",
                adapter_version="1.0.0",
                sampling_fingerprint="v1",
            ),
        )

        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.TRANSCRIPT_SEGMENT)

        assert is_valid is False
        assert "non-anonymous" in error


# ---------------------------------------------------------------------------
# 7.7 Cache reuse
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    settings = SQLiteSettings()
    db = WorkspaceSQLite(db_path, settings)
    yield db
    if db_path.exists():
        db_path.unlink()


class TestAudioCacheReuse:
    def test_identical_wav_content_reuses_metadata_representation(
        self, temp_db, resource_version_id
    ):
        registry = RepresentationRegistry(temp_db)
        cache = RepresentationCache(registry)

        settings = MediaSamplingSettings()
        content_hash = "b" * 32
        fingerprint = build_pipeline_fingerprint(
            source_content_hash=content_hash,
            representation_kind=MediaRepresentationKind.METADATA,
            stage=__import__(
                "katsi_core.media.contracts", fromlist=["PipelineStage"]
            ).PipelineStage.EXTRACT_METADATA,
            adapter_name="audio_metadata_wav",
            adapter_version="1.0.0",
            settings=settings,
        )

        from datetime import UTC, datetime
        from uuid import uuid4 as _uuid4

        from katsi_core.media.contracts import (
            DerivedRepresentation,
            MediaCoverage,
            WholeResourceLocator,
        )

        rep_id = _uuid4()
        representation = DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.METADATA,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            textual_payload=json.dumps(
                {
                    "container": "wav",
                    "codec": "pcm",
                    "duration_ms": 1000,
                    "channels": 1,
                    "sample_rate": 8000,
                }
            ),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="audio_metadata_wav",
                adapter_version="1.0.0",
            ),
            pipeline_fingerprint=fingerprint,
        )
        registry.register_representation(representation)

        # A second, distinct resource version with byte-identical content
        # (same source_content_hash / fingerprint) reuses the representation
        # instead of recomputing it.
        copied_resource_id = _uuid4()
        result = cache.find_compatible(
            copied_resource_id, MediaRepresentationKind.METADATA, fingerprint
        )

        assert result is not None
        assert result.representation.id == rep_id
        assert result.is_exact_resource_match is False

    def test_different_chunking_thresholds_do_not_share_cache_entries(self):
        default_settings = MediaSamplingSettings()
        custom_settings = MediaSamplingSettings(
            chunking=ChunkingThresholds(target_tokens=1024, overlap=32)
        )

        fp_default = build_pipeline_fingerprint(
            source_content_hash="c" * 32,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=__import__(
                "katsi_core.media.contracts", fromlist=["PipelineStage"]
            ).PipelineStage.TRANSCRIBE,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
            settings=default_settings,
        )
        fp_custom = build_pipeline_fingerprint(
            source_content_hash="c" * 32,
            representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            stage=__import__(
                "katsi_core.media.contracts", fromlist=["PipelineStage"]
            ).PipelineStage.TRANSCRIBE,
            adapter_name="audio_transcribe_whisper",
            adapter_version="1.0.0",
            settings=custom_settings,
        )

        assert fp_default.sampling_fingerprint != fp_custom.sampling_fingerprint
