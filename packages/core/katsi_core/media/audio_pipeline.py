"""Audio understanding pipelines: metadata, decode, transcription, speakers.

Implements Section 7 (Audio Understanding) of the multimedia-understanding
change:

- 7.1 deterministic audio metadata extraction (container/codec/duration/
  channels/sample rate) via in-process WAV header parsing -- no subprocess,
  same trust model as :mod:`katsi_core.media.detection`.
- 7.2 bounded local decoding to a private normalized working representation
  (mono 16kHz PCM WAV) via a ``ffmpeg``-family adapter routed through
  :class:`~katsi_core.media.execution.BoundedSubprocessExecutor`.
- 7.3 configured local speech transcription returning strict timestamped
  segments and coverage, via a ``whisper``-family adapter that emits a
  strictly validated JSON batch.
- 7.4 transcript chunk assembly that preserves each source segment's
  ``TimeRangeLocator`` and never duplicates evidence across chunks.
- 7.5 optional anonymous speaker segmentation: speaker labels are restricted
  to the ``SPEAKER_<n>`` pattern and are never treated as real identities.
- 7.6 explicit representation of silence/music/unsupported-language/
  unrecognized speech without fabricating transcript text.

Design note (task 7.4): unlike :func:`katsi_core.ingest.chunk.chunk`, which
duplicates trailing text across adjacent chunks as an overlap window,
transcript chunk assembly here performs a strict *partition* of segments --
every segment belongs to exactly one chunk and contributes exactly one
``TimeRangeLocator``. Character-level overlap would duplicate transcript
text (and therefore duplicate Claim evidence) across adjacent chunks, which
task 7.4 explicitly forbids for time-based segments.

All subprocess-backed adapters here only ever invoke
:class:`~katsi_core.media.execution.BoundedSubprocessExecutor` with an
owner-registered :class:`~katsi_core.media.contracts.MediaPipelineDefinition`
-- never a raw ``subprocess`` call -- per Decision 4 and the security
invariants documented in ``execution.py``.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from katsi_core.config import ChunkingThresholds
from katsi_core.ingest.chunk import estimate_tokens
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    ResourceVersionId,
    TimeRangeLocator,
    WholeResourceLocator,
)
from katsi_core.media.execution import (
    BoundedSubprocessExecutor,
    validate_json_output,
)
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

# =============================================================================
# 7.1 Deterministic audio metadata extraction (in-process, no subprocess)
# =============================================================================


class AudioMetadataError(ValueError):
    """Raised when audio container/header bytes cannot be parsed deterministically."""


@dataclass(frozen=True, slots=True)
class WavAudioInfo:
    """Deterministic metadata extracted from a WAV container header."""

    container: str
    codec: str
    duration_ms: int
    channels: int
    sample_rate: int
    bits_per_sample: int

    def to_json_payload(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "codec": self.codec,
            "duration_ms": self.duration_ms,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "bits_per_sample": self.bits_per_sample,
        }


_WAV_FORMAT_CODEC_NAMES = {
    1: "pcm",
    3: "ieee_float",
    6: "alaw",
    7: "mulaw",
}


def parse_wav_metadata(data: bytes) -> WavAudioInfo:
    """Deterministically parse container/codec/duration/channels/sample rate.

    Reads only the RIFF/WAVE chunk headers (``fmt `` and ``data``); never
    decodes audio samples. Raises :class:`AudioMetadataError` for malformed,
    truncated, or non-WAV input rather than guessing.
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioMetadataError("Not a RIFF/WAVE container")

    fmt: dict[str, int] | None = None
    data_bytes: int | None = None

    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", data, offset + 4)
        chunk_body_start = offset + 8

        if chunk_id == b"fmt ":
            if chunk_body_start + 16 > len(data):
                raise AudioMetadataError("Truncated fmt chunk")
            (
                audio_format,
                channels,
                sample_rate,
                _byte_rate,
                _block_align,
                bits_per_sample,
            ) = struct.unpack_from("<HHIIHH", data, chunk_body_start)
            if channels < 1:
                raise AudioMetadataError("Invalid channel count")
            if sample_rate < 1:
                raise AudioMetadataError("Invalid sample rate")
            if bits_per_sample < 1:
                raise AudioMetadataError("Invalid bits per sample")
            fmt = {
                "audio_format": audio_format,
                "channels": channels,
                "sample_rate": sample_rate,
                "bits_per_sample": bits_per_sample,
            }
        elif chunk_id == b"data":
            # The data chunk may legitimately be truncated relative to its
            # declared size (partial/interrupted capture); clamp to what is
            # actually present rather than raising, so partial-duration
            # files still produce a best-effort, honestly-bounded duration.
            available = max(0, len(data) - chunk_body_start)
            data_bytes = min(chunk_size, available)

        # Chunks are padded to even byte boundaries.
        offset = chunk_body_start + chunk_size + (chunk_size & 1)

    if fmt is None:
        raise AudioMetadataError("WAV container missing fmt chunk")
    if data_bytes is None:
        raise AudioMetadataError("WAV container missing data chunk")

    bytes_per_second = fmt["sample_rate"] * fmt["channels"] * (fmt["bits_per_sample"] // 8)
    duration_ms = 0 if bytes_per_second == 0 else round(data_bytes * 1000 / bytes_per_second)

    codec = _WAV_FORMAT_CODEC_NAMES.get(fmt["audio_format"], f"unknown_{fmt['audio_format']}")

    return WavAudioInfo(
        container="wav",
        codec=codec,
        duration_ms=duration_ms,
        channels=fmt["channels"],
        sample_rate=fmt["sample_rate"],
        bits_per_sample=fmt["bits_per_sample"],
    )


class AudioMetadataPipeline(MediaPipelineProtocol):
    """Deterministic WAV metadata extraction (task 7.1).

    Pure in-process header parsing -- never invokes a subprocess -- mirroring
    the trust model of :class:`~katsi_core.media.detection.ContentSignatureDetector`.
    A malformed/undecodable container raises, letting
    :class:`~katsi_core.media.execution.PipelineExecutionOrchestrator` produce
    a structured FAILED representation via its existing retry path.
    """

    _DEFINITION = MediaPipelineDefinition(
        id="audio_metadata_wav_v1",
        name="WAV audio metadata extraction",
        description="Deterministic container/codec/duration/channels/sample-rate extraction.",
        stage=PipelineStage.EXTRACT_METADATA,
        accepted_mime_patterns=["audio/wav", "audio/x-wav", "audio/wave"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.METADATA],
        producer_type=MediaProducerType.DETERMINISTIC,
        network_disabled=True,
        strict_output_contract=True,
        retry_on_failure=True,
    )

    @classmethod
    def get_adapter_name(cls) -> str:
        return "audio_metadata_wav"

    @classmethod
    def get_adapter_version(cls) -> str:
        return "1.0.0"

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return cls._DEFINITION

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        data = file_path.read_bytes()
        info = parse_wav_metadata(data)

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.METADATA,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(info.to_json_payload(), sort_keys=True),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
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
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful metadata extraction"
        if not output.textual_payload:
            return False, "Metadata representation must carry a JSON payload"
        try:
            parsed = json.loads(output.textual_payload)
        except json.JSONDecodeError as e:
            return False, f"Metadata payload is not valid JSON: {e}"
        required = {"container", "codec", "duration_ms", "channels", "sample_rate"}
        missing = required - parsed.keys()
        if missing:
            return False, f"Metadata payload missing keys: {sorted(missing)}"
        return True, None


# =============================================================================
# 7.2 Bounded local decoding to a private normalized working representation
# =============================================================================


def build_decode_definition(
    *,
    executable_path: str = "ffmpeg",
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 200_000_000,
    max_duration_ms: int | None = None,
) -> MediaPipelineDefinition:
    """Owner-configured definition for decoding to a normalized mono/16kHz WAV.

    The fixed argument template only ever substitutes the three allowed
    placeholders (`input_path`, `output_path`, `working_directory`); the
    executable identity and every other argument is fixed, owner-authored
    configuration (see ``execution.ALLOWED_ARG_PLACEHOLDERS``).
    """
    return MediaPipelineDefinition(
        id="audio_decode_ffmpeg_v1",
        name="Normalize audio to mono 16kHz PCM WAV",
        description="Bounded local decode to a private normalized working representation.",
        stage=PipelineStage.GENERATE_PROXY,
        accepted_mime_patterns=["audio/*"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.PROXY_MEDIA],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-y",
            "-i",
            "{input_path}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "{output_path}",
        ],
        allowed_env_vars=[],
        working_directory=".",
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_duration_ms=max_duration_ms,
        strict_output_contract=True,
        retry_on_failure=True,
    )


class AudioDecodePipeline(MediaPipelineProtocol):
    """Bounded local decode to a private normalized working representation (7.2).

    Never calls a binary directly: always routes through
    :class:`~katsi_core.media.execution.BoundedSubprocessExecutor` with a
    fixed, owner-authored :class:`MediaPipelineDefinition`. The normalized
    bytes are never returned inline (`blob_reference` is a content-addressed
    marker, not raw bytes) -- this is an internal working artifact consumed
    by downstream transcription, not a payload exposed to agents.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_decode_definition()
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "audio_decode_ffmpeg"

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
    ) -> DerivedRepresentation:
        output_path = working_directory / "normalized.wav"
        result = self._executor.execute(
            self._definition, file_path, working_directory, output_path=output_path
        )

        if result.timed_out or result.exit_code != 0 or not output_path.exists():
            raise RuntimeError(
                f"Decode failed: exit_code={result.exit_code} timed_out={result.timed_out} "
                f"stderr={result.stderr_sample[:500]!r}"
            )

        normalized_bytes = output_path.read_bytes()
        if not normalized_bytes:
            raise RuntimeError("Decode produced an empty normalized working representation")

        import hashlib

        digest = hashlib.sha256(normalized_bytes).hexdigest()

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.PROXY_MEDIA,
            media_type="audio/wav",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            blob_reference=f"private-audio-proxy:{digest}",
            blob_hash=digest,
            blob_byte_count=len(normalized_bytes),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
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
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful decode"
        if not output.blob_hash or not output.blob_byte_count:
            return False, "Decoded proxy representation must carry a non-empty blob"
        return True, None


# =============================================================================
# 7.2b Measured silence spans (deterministic, ffmpeg silencedetect)
# =============================================================================


# Real ffmpeg output (verified against ffmpeg 8.1.2) looks like:
#   [Parsed_silencedetect_0 @ 0x814c3c900] silence_start: 1.999955
#   [Parsed_silencedetect_0 @ 0x814c3c900] silence_end: 5 | silence_duration: 3.000045
# Start and end are always on separate lines, and a time may be formatted as a
# bare integer, hence the optional decimal part.
_SILENCE_START_PATTERN = re.compile(r"\bsilence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_PATTERN = re.compile(r"\bsilence_end:\s*(-?\d+(?:\.\d+)?)")


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
    track ended while still silent. Current ffmpeg closes such a span itself
    at EOF, but older builds do not, so that span closes at
    ``track_duration_ms`` rather than being dropped: trailing dead air is
    exactly what a caller wants to find. Never fabricates -- any structurally
    impossible pairing raises rather than being silently repaired.
    """
    if track_duration_ms <= 0:
        raise ValueError("track_duration_ms must be positive")

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


# =============================================================================
# 7.3 Configured local speech transcription: strict segments + coverage
# =============================================================================


_ALLOWED_SEGMENT_KINDS = {"speech", "silence", "music", "unrecognized", "unsupported_language"}


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


@dataclass(frozen=True, slots=True)
class TranscriptSegmentData:
    """One strictly-parsed timestamped transcription segment (7.3/7.6)."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None
    segment_kind: str  # one of _ALLOWED_SEGMENT_KINDS
    language: str | None = None
    speaker_label: str | None = None
    # Last field so existing positional construction stays valid.
    words: tuple[WordTimingData, ...] = ()


def build_transcribe_definition(
    *,
    executable_path: str = "whisper",
    timeout_seconds: float = 120.0,
    max_output_bytes: int = 20_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for local speech transcription.

    The configured executable is an owner-supplied wrapper, not the stock
    whisper CLI: the strict contract requires ``segments`` and
    ``coverage_fraction``, which stock whisper does not emit. With
    ``--word-timestamps`` the wrapper must additionally emit an optional
    per-segment ``words`` array of ``{start_ms, end_ms, text, confidence?}``,
    each word's range falling within its parent segment, time-ordered and
    non-overlapping. Speech segments only.
    """
    return MediaPipelineDefinition(
        id="audio_transcribe_whisper_v1",
        name="Local speech transcription",
        description="Configured local speech transcription with strict timestamped segments.",
        stage=PipelineStage.TRANSCRIBE,
        accepted_mime_patterns=["audio/*"],
        input_kinds=[MediaRepresentationKind.PROXY_MEDIA],
        representation_kinds_produced=[MediaRepresentationKind.TRANSCRIPT_SEGMENT],
        producer_type=MediaProducerType.MODEL_BACKED,
        executable_path=executable_path,
        fixed_args=["{input_path}", "--output_dir", "{working_directory}", "--word-timestamps"],
        allowed_env_vars=[],
        working_directory=".",
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


def _parse_transcription_batch(raw: str) -> dict[str, Any]:
    """Strictly parse a transcription tool's JSON batch output.

    Never fabricates: any structural problem raises rather than being
    silently filled in.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Transcription output is not valid JSON: {e}") from e

    is_valid, error = validate_json_output(
        parsed, required_keys={"segments", "coverage_fraction"}, expected_types={"segments": list}
    )
    if not is_valid:
        raise ValueError(f"Transcription output failed validation: {error}")
    return parsed


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


class AudioTranscriptionPipeline(MediaPipelineProtocol):
    """Local speech transcription producing a strictly validated raw batch (7.3).

    The subprocess call happens exactly once per attempt, via
    :class:`~katsi_core.media.execution.BoundedSubprocessExecutor`. The
    resulting representation carries the raw, strictly-validated JSON batch
    (segments + overall coverage); use :func:`parse_transcript_segments` and
    :func:`build_segment_representations` to expand it into the N per-segment
    representations described in design.md ("Representation(transcript_segment)
    x N").
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_transcribe_definition()
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "audio_transcribe_whisper"

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
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        result = self._executor.execute(self._definition, file_path, working_directory)

        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Transcription failed: exit_code={result.exit_code} timed_out={result.timed_out} "
                f"stderr={result.stderr_sample[:500]!r}"
            )

        batch = _parse_transcription_batch(result.stdout_sample)

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(batch, sort_keys=True),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(
                is_complete=float(batch["coverage_fraction"]) >= 1.0,
                coverage_fraction=float(batch["coverage_fraction"]),
                detail="raw transcription batch (see per-segment representations)",
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
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
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful transcription"
        if not output.textual_payload:
            return False, "Transcription batch representation must carry a JSON payload"
        try:
            _parse_transcription_batch(output.textual_payload)
        except ValueError as e:
            return False, str(e)
        return True, None


def parse_transcript_segments(batch: dict[str, Any]) -> tuple[list[TranscriptSegmentData], float]:
    """Strictly parse the raw transcription batch into typed segments.

    Returns the parsed segments (time-ordered as received) and the overall
    coverage fraction reported by the transcription tool. Raises
    :class:`ValueError` on any structurally invalid segment rather than
    silently dropping or fabricating data.
    """
    coverage_fraction = float(batch["coverage_fraction"])
    if not (0.0 <= coverage_fraction <= 1.0):
        raise ValueError("coverage_fraction must be within [0.0, 1.0]")

    segments: list[TranscriptSegmentData] = []
    for raw_segment in batch["segments"]:
        if not isinstance(raw_segment, dict):
            raise ValueError("Each segment must be a JSON object")

        start_ms = int(raw_segment["start_ms"])
        end_ms = int(raw_segment["end_ms"])
        if end_ms <= start_ms:
            raise ValueError(f"Segment end_ms ({end_ms}) must exceed start_ms ({start_ms})")

        segment_kind = raw_segment.get("segment_kind", "speech")
        if segment_kind not in _ALLOWED_SEGMENT_KINDS:
            raise ValueError(f"Unknown segment_kind: {segment_kind!r}")

        text = raw_segment.get("text", "")
        if not isinstance(text, str):
            raise ValueError("Segment text must be a string")
        # 7.6: silence/music/unrecognized/unsupported_language segments never
        # carry fabricated transcript text.
        if segment_kind != "speech" and text:
            raise ValueError(f"Non-speech segment_kind={segment_kind!r} must not carry text")

        confidence = raw_segment.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not (0.0 <= confidence <= 1.0):
                raise ValueError("confidence must be within [0.0, 1.0]")

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

    return segments, coverage_fraction


_SEGMENT_KIND_DETAIL = {
    "silence": "silence, no speech detected",
    "music": "non-speech audio (music) detected",
    "unrecognized": "speech present but not recognized",
    "unsupported_language": "speech in an unsupported language",
}


def build_segment_representations(
    segments: list[TranscriptSegmentData],
    resource_version_id: ResourceVersionId,
    pipeline_fingerprint: PipelineFingerprint,
    adapter: ProducerProvenance,
) -> list[DerivedRepresentation]:
    """Expand parsed segments into N per-segment representations (design.md:
    ``Representation(transcript_segment) x N``).

    Each segment becomes its own immutable representation carrying exactly
    one :class:`TimeRangeLocator`, so downstream chunk assembly (7.4) and
    Claim evidence always cite a single, unambiguous time range. Silence,
    music, unrecognized speech, and unsupported-language segments (7.6) get
    an empty ``textual_payload`` and an explanatory ``coverage.detail``
    instead of fabricated transcript text.
    """
    now = datetime.now(UTC)
    representations: list[DerivedRepresentation] = []

    for segment in segments:
        rep_id = uuid4()
        locator = TimeRangeLocator(
            resource_version_id=resource_version_id,
            representation_id=rep_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
        )

        text = segment.text
        if segment.speaker_label:
            text = f"[{segment.speaker_label}] {text}" if text else text

        if segment.segment_kind == "speech":
            status = MediaRepresentationStatus.CURRENT
            coverage = MediaCoverage(is_complete=True, coverage_fraction=1.0, detail="speech")
        else:
            # Silence/music are accurately-identified non-speech intervals,
            # not failures: CURRENT status, but zero *speech* coverage and no
            # fabricated text. Unrecognized/unsupported-language speech is
            # PARTIAL: speech is present but not transcribable.
            status = (
                MediaRepresentationStatus.CURRENT
                if segment.segment_kind in {"silence", "music"}
                else MediaRepresentationStatus.PARTIAL
            )
            coverage = MediaCoverage(
                is_complete=segment.segment_kind in {"silence", "music"},
                coverage_fraction=1.0 if segment.segment_kind in {"silence", "music"} else 0.0,
                detail=_SEGMENT_KIND_DETAIL[segment.segment_kind]
                + (f" ({segment.language})" if segment.language else ""),
            )

        representations.append(
            DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
                media_type="text/plain",
                status=status,
                created_at=now,
                updated_at=now,
                textual_payload=text,
                locators=(locator,),
                coverage=coverage,
                confidence=segment.confidence if segment.segment_kind == "speech" else None,
                producer=adapter,
                pipeline_fingerprint=pipeline_fingerprint,
                error=None,
            )
        )

    return representations


# =============================================================================
# 7.4 Transcript chunk assembly: preserve locators, no duplicate evidence
# =============================================================================


def assemble_transcript_chunks(
    segment_representations: list[DerivedRepresentation],
    *,
    settings: ChunkingThresholds | None = None,
) -> list[DerivedRepresentation]:
    """Assemble transcript segments into retrieval-sized chunks (7.4).

    Groups consecutive **speech** segment representations (identified by a
    non-empty ``textual_payload`` and exactly one ``TimeRangeLocator``) up to
    ``settings.target_tokens``. Unlike :func:`katsi_core.ingest.chunk.chunk`,
    this performs a strict partition: every source segment contributes to
    exactly one chunk and exactly one ``TimeRangeLocator``, with no
    character-level overlap between adjacent chunks. Overlap would duplicate
    transcript text -- and therefore duplicate Claim evidence -- across
    chunks, which this task explicitly forbids for time-based segments.

    Non-speech segments (silence/music/unrecognized/unsupported_language,
    empty ``textual_payload``) are never merged into a chunk; they remain
    standalone representations so their absence of text is never disguised
    by concatenation.
    """
    if settings is None:
        settings = ChunkingThresholds()

    speech_segments = [
        rep
        for rep in segment_representations
        if rep.textual_payload and rep.status == MediaRepresentationStatus.CURRENT
    ]
    if not speech_segments:
        return []

    target_tokens = settings.target_tokens
    now = datetime.now(UTC)
    chunks: list[DerivedRepresentation] = []

    current_group: list[DerivedRepresentation] = []
    current_tokens = 0

    def _flush() -> None:
        if not current_group:
            return
        chunk_id = uuid4()
        text = " ".join(rep.textual_payload or "" for rep in current_group)
        locators = tuple(rep.locators[0] for rep in current_group)
        # Every locator in the chunk must be traceable to a distinct source
        # segment and no two chunks may repeat a locator (no duplicate
        # overlapping Claim evidence).
        first = current_group[0]
        total_ms = sum(
            loc.end_ms - loc.start_ms for loc in locators if isinstance(loc, TimeRangeLocator)
        )
        covered_ms = sum(
            (loc.end_ms - loc.start_ms)
            for rep, loc in zip(current_group, locators, strict=True)
            if isinstance(loc, TimeRangeLocator) and rep.coverage.is_complete
        )
        coverage_fraction = 1.0 if total_ms == 0 else covered_ms / total_ms

        chunks.append(
            DerivedRepresentation(
                id=chunk_id,
                resource_version_id=first.resource_version_id,
                kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
                media_type="text/plain",
                status=MediaRepresentationStatus.CURRENT,
                created_at=now,
                updated_at=now,
                textual_payload=text,
                locators=locators,
                coverage=MediaCoverage(
                    is_complete=coverage_fraction >= 1.0,
                    coverage_fraction=coverage_fraction,
                    detail=f"assembled from {len(current_group)} source segment(s)",
                ),
                producer=ProducerProvenance(
                    producer_type=MediaProducerType.DETERMINISTIC,
                    adapter_name="transcript_chunk_assembly",
                    adapter_version="1.0.0",
                ),
                pipeline_fingerprint=first.pipeline_fingerprint,
            )
        )

    for rep in speech_segments:
        rep_tokens = estimate_tokens(rep.textual_payload or "")
        if current_group and current_tokens + rep_tokens > target_tokens:
            _flush()
            current_group = []
            current_tokens = 0
        current_group.append(rep)
        current_tokens += rep_tokens

    _flush()

    # Structural guarantee (not just a convention): no TimeRangeLocator
    # (by identity of resource_version_id/start_ms/end_ms) appears in more
    # than one chunk.
    seen: set[tuple[str, int, int]] = set()
    for chunk in chunks:
        for loc in chunk.locators:
            if isinstance(loc, TimeRangeLocator):
                key = (str(loc.resource_version_id), loc.start_ms, loc.end_ms)
                if key in seen:
                    raise AssertionError("Duplicate overlapping Claim evidence across chunks")
                seen.add(key)

    return chunks


# =============================================================================
# 7.5 Optional anonymous speaker segmentation (no real-identity inference)
# =============================================================================


SPEAKER_LABEL_PATTERN = re.compile(r"^SPEAKER_\d+$")


@dataclass(frozen=True, slots=True)
class SpeakerSegmentData:
    """One strictly-parsed anonymous speaker interval."""

    start_ms: int
    end_ms: int
    speaker_label: str


def build_speaker_segmentation_definition(
    *,
    executable_path: str = "pyannote-cli",
    timeout_seconds: float = 120.0,
    max_output_bytes: int = 5_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for anonymous speaker segmentation."""
    return MediaPipelineDefinition(
        id="audio_speaker_segmentation_v1",
        name="Anonymous speaker segmentation",
        description=(
            "Optional anonymous speaker segmentation. Labels are ephemeral, "
            "resource-scoped SPEAKER_<n> ids only -- never real identities."
        ),
        stage=PipelineStage.SEGMENT_SPEAKERS,
        accepted_mime_patterns=["audio/*"],
        input_kinds=[MediaRepresentationKind.PROXY_MEDIA],
        representation_kinds_produced=[MediaRepresentationKind.TRANSCRIPT_SEGMENT],
        producer_type=MediaProducerType.MODEL_BACKED,
        executable_path=executable_path,
        fixed_args=["{input_path}"],
        allowed_env_vars=[],
        working_directory=".",
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


class AudioSpeakerSegmentationPipeline(MediaPipelineProtocol):
    """Optional anonymous speaker segmentation (7.5).

    ``validate_output`` is the enforcement point for "no real-world voice
    identity inference": any speaker label that does not match
    ``SPEAKER_<n>`` (e.g. a real name a model hallucinated) fails validation
    and is never accepted as representation content.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_speaker_segmentation_definition()
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "audio_speaker_segmentation"

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
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        result = self._executor.execute(self._definition, file_path, working_directory)

        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Speaker segmentation failed: exit_code={result.exit_code} "
                f"timed_out={result.timed_out} stderr={result.stderr_sample[:500]!r}"
            )

        try:
            parsed = json.loads(result.stdout_sample)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Speaker segmentation output is not valid JSON: {e}") from e

        is_valid, error = validate_json_output(
            parsed, required_keys={"speaker_segments"}, expected_types={"speaker_segments": list}
        )
        if not is_valid:
            raise RuntimeError(f"Speaker segmentation output failed validation: {error}")

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(parsed, sort_keys=True),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
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
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful speaker segmentation"
        if not output.textual_payload:
            return False, "Speaker segmentation representation must carry a JSON payload"
        try:
            parsed = json.loads(output.textual_payload)
        except json.JSONDecodeError as e:
            return False, f"Speaker segmentation payload is not valid JSON: {e}"

        for raw_segment in parsed.get("speaker_segments", []):
            label = raw_segment.get("speaker_label", "")
            if not SPEAKER_LABEL_PATTERN.match(label):
                return False, (
                    f"Rejected non-anonymous or malformed speaker label {label!r}: "
                    "only SPEAKER_<n> labels are permitted (no real-world voice identity)"
                )
        return True, None


def parse_speaker_segments(payload: dict[str, Any]) -> list[SpeakerSegmentData]:
    """Strictly parse speaker segmentation output, enforcing anonymous labels."""
    segments: list[SpeakerSegmentData] = []
    for raw in payload["speaker_segments"]:
        start_ms = int(raw["start_ms"])
        end_ms = int(raw["end_ms"])
        if end_ms <= start_ms:
            raise ValueError(f"Speaker segment end_ms ({end_ms}) must exceed start_ms ({start_ms})")
        label = raw["speaker_label"]
        if not SPEAKER_LABEL_PATTERN.match(label):
            raise ValueError(f"Rejected non-anonymous speaker label: {label!r}")
        segments.append(SpeakerSegmentData(start_ms=start_ms, end_ms=end_ms, speaker_label=label))
    return segments


def apply_speaker_labels(
    segments: list[TranscriptSegmentData], speaker_segments: list[SpeakerSegmentData]
) -> list[TranscriptSegmentData]:
    """Assign an anonymous speaker label to each transcript segment by maximal
    time overlap with a speaker interval.

    Segments with no overlapping speaker data keep ``speaker_label=None`` --
    a missing speaker assignment is never guessed.
    """
    labeled: list[TranscriptSegmentData] = []
    for segment in segments:
        best_label: str | None = None
        best_overlap = 0
        for speaker in speaker_segments:
            overlap = min(segment.end_ms, speaker.end_ms) - max(segment.start_ms, speaker.start_ms)
            if overlap > best_overlap or (
                overlap == best_overlap
                and overlap > 0
                and best_label is not None
                and speaker.start_ms < segment.start_ms
            ):
                best_overlap = overlap
                best_label = speaker.speaker_label
        labeled.append(
            TranscriptSegmentData(
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text,
                confidence=segment.confidence,
                segment_kind=segment.segment_kind,
                language=segment.language,
                speaker_label=best_label if best_overlap > 0 else None,
            )
        )
    return labeled
