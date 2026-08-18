"""Video understanding pipeline: metadata, coverage planning, scenes, keyframes.

Implements openspec change ``multimedia-understanding`` tasks.md Section 8
and design.md Decision 8 ("Video processing is budgeted sampling, not
exhaustive frames").

The centerpiece is :class:`VideoCoveragePlanner` (task 8.2): before any frame
is decoded, it computes an explicit coverage plan bounded by configured
duration, keyframe count, decoded-pixel, output-byte, wall-time, and compute
class budgets. When a full-density plan does not fit, the planner never
silently samples a short prefix and reports the video as understood -- it
either produces a reduced-density plan that still spans the *entire*
duration, escalates to ``OWNER_APPROVAL_REQUIRED``, or reports
``UNAVAILABLE``. See design.md around "The sampling planner calculates an
explicit coverage plan before decoding frames."

Decoding, scene-boundary detection, and keyframe extraction are bounded
subprocess adapters (ffmpeg-family tools) that go through
``MediaPipelineProtocol`` + ``PipelineExecutionOrchestrator`` /
``BoundedSubprocessExecutor`` exactly like every other media pipeline in
this package -- this module never calls a binary directly. No new
third-party dependency is imported at module scope (Decision 15); heavy
video libraries, if ever used, stay behind an optional adapter boundary.

Reconciliation notes for Section 7 (audio) and Section 5 (image), built
concurrently by other agents -- both now exist in this tree and were
confirmed against this module:

- Section 7's ``AudioTranscriptionPipeline`` (``katsi_core.media.audio_pipeline``)
  turned out to be a standard ``MediaPipelineProtocol`` adapter
  (``process(file_path, ...)`` returning one representation carrying a raw
  JSON transcription batch), not the bytes/segments-returning
  ``AudioTranscriptionAdapter`` Protocol originally guessed below. That
  Protocol is kept as the *abstract* shape this module needs (and is what
  :func:`transcribe_video_audio_track` and its tests use), while
  :func:`transcribe_video_audio_track_via_audio_pipeline` is the concrete
  integration that runs the real pipeline through
  ``PipelineExecutionOrchestrator`` and expands it with
  ``audio_pipeline.parse_transcript_segments`` /
  ``audio_pipeline.build_segment_representations``.
- Section 5's ``ImageCaptionPipeline`` / ``ImageVisualEmbeddingPipeline``
  (``katsi_core.media.image_pipeline``) are likewise standard
  ``MediaPipelineProtocol`` adapters operating on a file path, not the
  bytes-based ``ImageCaptionAdapter`` / ``ImageEmbeddingAdapter`` Protocols
  below. :func:`caption_keyframe_via_image_pipeline` /
  :func:`embed_keyframe_via_image_pipeline` are the concrete integrations.
- Both real pipelines persist derived blobs through a ``BlobStore`` passed
  into their constructor (see ``image_pipeline.ImageThumbnailPipeline``);
  :class:`VideoAudioExtractionPipeline` and :class:`KeyframeExtractionPipeline`
  below now follow the same convention so their ``blob_reference`` stays
  valid after the orchestrator's per-attempt temp directory is removed.

Every integration point remains optional: a missing module, a missing
adapter, or an adapter failure degrades to an explicit
``UNAVAILABLE``/``None``/skipped result, never a fabricated one.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from katsi_core.config import MediaSamplingSettings
from katsi_core.media.blob_store import BlobStore
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
    RepresentationError,
    ResourceVersionId,
    SceneLocator,
    TimeRangeLocator,
    VideoFrameLocator,
)
from katsi_core.media.execution import BoundedSubprocessExecutor, PipelineExecutionOrchestrator
from katsi_core.media.fingerprint import build_pipeline_fingerprint
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

# =============================================================================
# 8.1 Deterministic video metadata extraction
# =============================================================================


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    """Deterministic, parsed video container/stream metadata.

    Produced by parsing a container-inspection tool's structured output
    (e.g. ffprobe ``-print_format json``). Parsing itself never executes
    embedded content and never trusts extension over inspected content.
    """

    duration_ms: int
    width: int | None
    height: int | None
    frame_rate: float | None
    is_variable_frame_rate: bool
    codec: str | None
    container: str | None
    has_audio: bool
    audio_codec: str | None
    encrypted: bool = False
    malformed: bool = False
    password_protected: bool = False

    @property
    def pixels_per_frame(self) -> int:
        if not self.width or not self.height:
            return 0
        return self.width * self.height

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "frame_rate": self.frame_rate,
            "is_variable_frame_rate": self.is_variable_frame_rate,
            "codec": self.codec,
            "container": self.container,
            "has_audio": self.has_audio,
            "audio_codec": self.audio_codec,
            "encrypted": self.encrypted,
            "malformed": self.malformed,
            "password_protected": self.password_protected,
        }


def _parse_frame_rate(rate: str | None) -> float | None:
    """Parse an ffprobe-style rational frame rate string (e.g. "30000/1001")."""
    if not rate:
        return None
    if "/" in rate:
        num_str, _, den_str = rate.partition("/")
        try:
            num, den = float(num_str), float(den_str)
        except ValueError:
            return None
        if den == 0:
            return None
        return num / den
    try:
        return float(rate)
    except ValueError:
        return None


def parse_video_stream_metadata(raw: dict[str, Any]) -> VideoStreamInfo:
    """Parse an ffprobe-shaped JSON payload into :class:`VideoStreamInfo`.

    Expects the standard ``ffprobe -show_format -show_streams -of json``
    shape: ``{"format": {...}, "streams": [{"codec_type": "video", ...}, ...]}``.
    Malformed or missing fields degrade to ``None``/``False`` rather than
    raising, since a partially-readable container is still worth an explicit
    (bounded) coverage decision rather than a hard failure.
    """
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_s: float | None = None
    for source in (video_stream, fmt):
        if source and source.get("duration") not in (None, "N/A"):
            try:
                duration_s = float(source["duration"])
                break
            except (TypeError, ValueError):
                continue
    duration_ms = max(0, round((duration_s or 0.0) * 1000))

    width = height = None
    codec = container = None
    r_frame_rate = avg_frame_rate = None
    if video_stream is not None:
        width = video_stream.get("width")
        height = video_stream.get("height")
        codec = video_stream.get("codec_name")
        r_frame_rate = _parse_frame_rate(video_stream.get("r_frame_rate"))
        avg_frame_rate = _parse_frame_rate(video_stream.get("avg_frame_rate"))
    container = fmt.get("format_name")

    is_vfr = (
        r_frame_rate is not None
        and avg_frame_rate is not None
        and not math.isclose(r_frame_rate, avg_frame_rate, rel_tol=0.01)
    )

    tags = fmt.get("tags") or {}
    encrypted = bool(fmt.get("encrypted")) or "encryption" in json.dumps(tags).lower()

    return VideoStreamInfo(
        duration_ms=duration_ms,
        width=width,
        height=height,
        frame_rate=avg_frame_rate or r_frame_rate,
        is_variable_frame_rate=is_vfr,
        codec=codec,
        container=container,
        has_audio=audio_stream is not None,
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        encrypted=encrypted,
        malformed=video_stream is None and audio_stream is None,
        password_protected=bool(fmt.get("format_long_name", "").lower().count("encrypted")),
    )


def build_video_metadata_definition(
    *, executable_path: str = "ffprobe", pipeline_id: str = "video_metadata_ffprobe_v1"
) -> MediaPipelineDefinition:
    """Owner-registerable definition for deterministic stream metadata extraction."""
    return MediaPipelineDefinition(
        id=pipeline_id,
        name="Video stream metadata (ffprobe)",
        description="Deterministic container/stream inspection; never executes content.",
        stage=PipelineStage.EXTRACT_METADATA,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.MEDIA_DESCRIPTOR],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "{input_path}",
        ],
        network_disabled=True,
        timeout_seconds=30.0,
        max_output_bytes=2_000_000,
        retry_on_failure=True,
    )


class VideoMetadataPipeline(MediaPipelineProtocol):
    """Deterministic video metadata extraction adapter (task 8.1).

    Runs the owner-registered probe command via :class:`BoundedSubprocessExecutor`
    and parses its JSON output with :func:`parse_video_stream_metadata`. Never
    calls a subprocess directly outside of the bounded executor.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_video_metadata_definition()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "video_metadata_ffprobe"

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
        result = BoundedSubprocessExecutor().execute(self._definition, file_path, working_directory)
        now = datetime.now(UTC)
        producer = ProducerProvenance(
            producer_type=self._definition.producer_type,
            adapter_name=self.get_adapter_name(),
            adapter_version=self.get_adapter_version(),
        )
        rep_id = uuid4()

        if result.exit_code != 0 or result.timed_out:
            return DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.MEDIA_DESCRIPTOR,
                media_type="application/json",
                status=MediaRepresentationStatus.FAILED,
                created_at=now,
                updated_at=now,
                coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
                producer=producer,
                pipeline_fingerprint=pipeline_fingerprint,
                error=RepresentationError(
                    error_category="processing_error",
                    error_message=result.stderr_sample or "probe failed",
                    is_retriable=not result.timed_out,
                ),
            )

        try:
            raw = json.loads(result.stdout_sample)
            info = parse_video_stream_metadata(raw)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            return DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.MEDIA_DESCRIPTOR,
                media_type="application/json",
                status=MediaRepresentationStatus.FAILED,
                created_at=now,
                updated_at=now,
                coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
                producer=producer,
                pipeline_fingerprint=pipeline_fingerprint,
                error=RepresentationError(
                    error_category="invalid_output",
                    error_message=f"could not parse probe output: {exc}",
                    is_retriable=False,
                ),
            )

        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.MEDIA_DESCRIPTOR,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(info.to_json_dict(), sort_keys=True),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, f"expected DerivedRepresentation, got {type(output).__name__}"
        if output.kind != representation_kind:
            return False, f"expected kind {representation_kind}, got {output.kind}"
        if output.status == MediaRepresentationStatus.CURRENT and output.textual_payload is None:
            return False, "current metadata representation must carry textual_payload"
        return True, None


# =============================================================================
# 8.2 Pre-decode coverage planner (the critical, design-highlighted decision)
# =============================================================================


class VideoCoveragePolicy(StrEnum):
    """The three policy outcomes design.md Decision 8 allows -- never a
    silent prefix sample reported as full understanding."""

    BOUNDED_SAMPLING = "bounded_sampling"
    OWNER_APPROVAL_REQUIRED = "owner_approval_required"
    UNAVAILABLE = "unavailable"


class VideoComputeClass(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class VideoCoverageBudget(BaseModel):
    """Configured budgets the sampling planner enforces before decoding.

    Every field here is a distinct budget dimension called out in
    design.md: duration, keyframes, decoded pixels, output bytes, wall
    time, and compute class.
    """

    max_duration_ms: int = Field(gt=0, description="Duration budget before owner approval")
    hard_max_duration_ms: int = Field(
        gt=0, description="Absolute duration ceiling; beyond this always UNAVAILABLE"
    )
    max_keyframes: int = Field(gt=0)
    max_decoded_pixels: int = Field(gt=0, description="Total pixels across all decoded keyframes")
    max_output_bytes: int = Field(gt=0)
    max_wall_time_seconds: float = Field(gt=0)
    allowed_compute_classes: frozenset[VideoComputeClass] = Field(
        default_factory=lambda: frozenset({VideoComputeClass.CPU})
    )
    gpu_required_pixel_threshold: int = Field(
        default=8_294_400,  # 4K frame area; below this CPU decode is assumed adequate
        gt=0,
    )
    min_scene_interval_ms: int = Field(default=2_000, gt=0)
    max_scene_interval_ms: int = Field(
        default=15_000, gt=0, description="Maximum-interval fallback for scene sampling (8.4)"
    )
    avg_keyframe_bytes: int = Field(default=150_000, gt=0)
    seconds_per_keyframe_decode: float = Field(default=0.05, gt=0)
    scene_detection_seconds_per_ms: float = Field(default=0.00002, gt=0)
    min_viable_keyframes: int = Field(default=1, gt=0)
    allow_owner_approval: bool = Field(default=True)
    approval_escalation_multiplier: float = Field(
        default=3.0, gt=1.0, description="Beyond max_duration * this, always UNAVAILABLE"
    )

    @model_validator(mode="after")
    def validate_intervals(self) -> VideoCoverageBudget:
        if self.min_scene_interval_ms > self.max_scene_interval_ms:
            raise ValueError("min_scene_interval_ms must not exceed max_scene_interval_ms")
        if self.max_duration_ms > self.hard_max_duration_ms:
            raise ValueError("max_duration_ms must not exceed hard_max_duration_ms")
        return self


@dataclass(frozen=True, slots=True)
class VideoCoveragePlan:
    """An explicit, pre-decode coverage plan (task 8.2)."""

    policy: VideoCoveragePolicy
    coverage: MediaCoverage
    planned_duration_ms: int = 0
    scene_interval_ms: int = 0
    planned_keyframe_count: int = 0
    estimated_decoded_pixels: int = 0
    estimated_output_bytes: int = 0
    estimated_wall_time_seconds: float = 0.0
    compute_class: VideoComputeClass = VideoComputeClass.CPU
    reasons: tuple[str, ...] = ()


def _unavailable_plan(reasons: Sequence[str]) -> VideoCoveragePlan:
    return VideoCoveragePlan(
        policy=VideoCoveragePolicy.UNAVAILABLE,
        coverage=MediaCoverage(
            is_complete=False, coverage_fraction=0.0, detail="; ".join(reasons) or "unavailable"
        ),
        reasons=tuple(reasons),
    )


def _owner_approval_plan(
    reasons: Sequence[str],
    *,
    requested_duration_ms: int,
    compute_class: VideoComputeClass = VideoComputeClass.CPU,
) -> VideoCoveragePlan:
    return VideoCoveragePlan(
        policy=VideoCoveragePolicy.OWNER_APPROVAL_REQUIRED,
        coverage=MediaCoverage(
            is_complete=False,
            coverage_fraction=0.0,
            detail="; ".join(reasons) or "owner approval required",
        ),
        planned_duration_ms=requested_duration_ms,
        compute_class=compute_class,
        reasons=tuple(reasons),
    )


class VideoCoveragePlanner:
    """Computes an explicit pre-decode coverage plan bounded by configured budgets.

    The planner always reasons about the *entire* source duration -- it
    never proposes sampling only a leading prefix. When full keyframe
    density does not fit the budgets, it reduces density while still
    spanning the whole duration; only when even a minimally viable plan
    cannot fit does it escalate to owner approval or report unavailable.
    """

    def plan(self, metadata: VideoStreamInfo, budget: VideoCoverageBudget) -> VideoCoveragePlan:
        if metadata.duration_ms <= 0:
            return _unavailable_plan(["source has no measurable duration"])
        if not metadata.width or not metadata.height:
            return _unavailable_plan(["source is missing frame dimensions"])
        if metadata.encrypted or metadata.password_protected:
            return _unavailable_plan(["source is encrypted or password protected"])
        if metadata.malformed:
            return _unavailable_plan(["source stream metadata is malformed"])

        pixels_per_frame = metadata.pixels_per_frame
        required_compute_class = (
            VideoComputeClass.GPU
            if pixels_per_frame >= budget.gpu_required_pixel_threshold
            else VideoComputeClass.CPU
        )
        if required_compute_class not in budget.allowed_compute_classes:
            reason = (
                f"{pixels_per_frame}px/frame requires {required_compute_class.value} decode, "
                f"not permitted by configured compute policy"
            )
            if budget.allow_owner_approval:
                return _owner_approval_plan(
                    [reason],
                    requested_duration_ms=metadata.duration_ms,
                    compute_class=required_compute_class,
                )
            return _unavailable_plan([reason])

        if metadata.duration_ms > budget.hard_max_duration_ms:
            return _unavailable_plan(
                [
                    f"duration {metadata.duration_ms}ms exceeds hard ceiling "
                    f"{budget.hard_max_duration_ms}ms"
                ]
            )

        if metadata.duration_ms > budget.max_duration_ms:
            reason = (
                f"duration {metadata.duration_ms}ms exceeds configured budget "
                f"{budget.max_duration_ms}ms"
            )
            escalation_ceiling_ms = budget.max_duration_ms * budget.approval_escalation_multiplier
            if metadata.duration_ms > escalation_ceiling_ms:
                return _unavailable_plan([reason, "exceeds owner-approval escalation multiplier"])
            if not budget.allow_owner_approval:
                return _unavailable_plan([reason, "owner approval is disabled"])
            return _owner_approval_plan(
                [reason, "owner approval required to sample the full duration"],
                requested_duration_ms=metadata.duration_ms,
                compute_class=required_compute_class,
            )

        target_duration_ms = metadata.duration_ms
        reasons: list[str] = []

        desired_keyframes = max(1, math.ceil(target_duration_ms / budget.min_scene_interval_ms))
        desired_keyframes = min(desired_keyframes, budget.max_keyframes)
        if desired_keyframes < math.ceil(target_duration_ms / budget.min_scene_interval_ms):
            reasons.append("max_keyframes budget below full min-interval density")

        effective_keyframes = desired_keyframes

        if pixels_per_frame > 0:
            pixel_limited = budget.max_decoded_pixels // pixels_per_frame
            if pixel_limited < effective_keyframes:
                reasons.append("reduced keyframe count to respect decoded-pixel budget")
            effective_keyframes = min(effective_keyframes, max(pixel_limited, 0))

        byte_limited = budget.max_output_bytes // budget.avg_keyframe_bytes
        if byte_limited < effective_keyframes:
            reasons.append("reduced keyframe count to respect output-byte budget")
        effective_keyframes = min(effective_keyframes, max(byte_limited, 0))

        scan_overhead_seconds = target_duration_ms * budget.scene_detection_seconds_per_ms

        def _wall_time(keyframe_count: int) -> float:
            return scan_overhead_seconds + keyframe_count * budget.seconds_per_keyframe_decode

        if scan_overhead_seconds > budget.max_wall_time_seconds:
            reason = "scene-scan overhead alone exceeds wall-time budget"
            if budget.allow_owner_approval:
                return _owner_approval_plan(
                    [reason],
                    requested_duration_ms=target_duration_ms,
                    compute_class=required_compute_class,
                )
            return _unavailable_plan([reason])

        if _wall_time(effective_keyframes) > budget.max_wall_time_seconds:
            reasons.append("reduced keyframe count to respect wall-time budget")
            remaining_seconds = budget.max_wall_time_seconds - scan_overhead_seconds
            time_limited = int(remaining_seconds / budget.seconds_per_keyframe_decode)
            effective_keyframes = min(effective_keyframes, max(time_limited, 0))

        if effective_keyframes < budget.min_viable_keyframes:
            reason = "computed budgets leave no viable keyframes across the full duration"
            if budget.allow_owner_approval:
                return _owner_approval_plan(
                    [reason],
                    requested_duration_ms=target_duration_ms,
                    compute_class=required_compute_class,
                )
            return _unavailable_plan([reason])

        scene_interval_ms = max(
            budget.min_scene_interval_ms,
            min(budget.max_scene_interval_ms, target_duration_ms // effective_keyframes),
        )

        is_full_density = effective_keyframes >= desired_keyframes and not reasons
        coverage_fraction = (
            1.0
            if is_full_density
            else min(round(effective_keyframes / desired_keyframes, 4), 0.9999)
        )
        coverage = MediaCoverage(
            is_complete=is_full_density,
            coverage_fraction=coverage_fraction,
            detail=(
                "full keyframe density achieved across entire duration"
                if is_full_density
                else (
                    f"reduced to {effective_keyframes}/{desired_keyframes} keyframes across the "
                    f"full {target_duration_ms}ms duration ({'; '.join(reasons)})"
                )
            ),
        )

        return VideoCoveragePlan(
            policy=VideoCoveragePolicy.BOUNDED_SAMPLING,
            coverage=coverage,
            planned_duration_ms=target_duration_ms,
            scene_interval_ms=scene_interval_ms,
            planned_keyframe_count=effective_keyframes,
            estimated_decoded_pixels=effective_keyframes * pixels_per_frame,
            estimated_output_bytes=effective_keyframes * budget.avg_keyframe_bytes,
            estimated_wall_time_seconds=_wall_time(effective_keyframes),
            compute_class=required_compute_class,
            reasons=tuple(reasons),
        )


# =============================================================================
# 8.3 Local audio-track extraction + reuse of the audio transcription pipeline
# =============================================================================


@runtime_checkable
class AudioTranscriptionAdapter(Protocol):
    """Structural shape expected from Section 7's audio transcription pipeline.

    RECONCILIATION NOTE: if ``katsi_core.media.audio_pipeline`` defines a
    differently-shaped entry point, adapt the call site in
    :func:`transcribe_video_audio_track` rather than this Protocol, since
    this module has no control over the audio pipeline's final API.
    """

    def transcribe(
        self,
        audio_path: Path,
        *,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        working_directory: Path,
    ) -> list[DerivedRepresentation]:
        """Return TRANSCRIPT_SEGMENT representations with time-locator evidence."""
        ...


def build_audio_track_extraction_definition(
    *, executable_path: str = "ffmpeg", pipeline_id: str = "video_audio_extract_v1"
) -> MediaPipelineDefinition:
    """Owner-registerable definition for local audio-track extraction to WAV."""
    return MediaPipelineDefinition(
        id=pipeline_id,
        name="Video audio-track extraction (ffmpeg)",
        description="Extracts the audio track to a local WAV file for transcription.",
        stage=PipelineStage.GENERATE_PROXY,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.PROXY_MEDIA],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-y",
            "-i",
            "{input_path}",
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "{output_path}",
        ],
        network_disabled=True,
        timeout_seconds=60.0,
        max_output_bytes=200_000_000,
    )


class VideoAudioExtractionPipeline(MediaPipelineProtocol):
    """Extracts the local audio track to a private WAV proxy blob (task 8.3).

    Mirrors the established convention in ``image_pipeline.ImageThumbnailPipeline``:
    the extracted bytes are persisted through a :class:`BlobStore` *before*
    the orchestrator's per-attempt temporary directory is removed, so
    ``blob_reference`` stays valid after ``process()`` returns.
    """

    def __init__(
        self,
        definition: MediaPipelineDefinition | None = None,
        blob_store: BlobStore | None = None,
    ) -> None:
        self._definition = definition or build_audio_track_extraction_definition()
        self._blob_store = blob_store

    @classmethod
    def get_adapter_name(cls) -> str:
        return "video_audio_extract_ffmpeg"

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
        output_path = working_directory / "audio_track.wav"
        result = BoundedSubprocessExecutor().execute(
            self._definition, file_path, working_directory, output_path=output_path
        )
        now = datetime.now(UTC)
        producer = ProducerProvenance(
            producer_type=self._definition.producer_type,
            adapter_name=self.get_adapter_name(),
            adapter_version=self.get_adapter_version(),
        )
        rep_id = uuid4()

        if result.exit_code != 0 or result.timed_out or not output_path.exists():
            return DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.PROXY_MEDIA,
                media_type="audio/wav",
                status=MediaRepresentationStatus.FAILED,
                created_at=now,
                updated_at=now,
                blob_reference="unavailable",
                blob_hash="0" * 32,
                blob_byte_count=0,
                coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
                producer=producer,
                pipeline_fingerprint=pipeline_fingerprint,
                error=RepresentationError(
                    error_category="processing_error",
                    error_message=result.stderr_sample or "audio extraction failed",
                    is_retriable=not result.timed_out,
                ),
            )

        if self._blob_store is None:
            raise RuntimeError(
                "VideoAudioExtractionPipeline requires a blob_store to persist output"
            )

        audio_bytes = output_path.read_bytes()
        blob_hash, byte_count = self._blob_store.store_blob(audio_bytes)
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.PROXY_MEDIA,
            media_type="audio/wav",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            blob_reference=f"blob:{blob_hash}",
            blob_hash=blob_hash,
            blob_byte_count=byte_count,
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, f"expected DerivedRepresentation, got {type(output).__name__}"
        if output.kind != representation_kind:
            return False, f"expected kind {representation_kind}, got {output.kind}"
        return True, None


@dataclass(frozen=True, slots=True)
class VideoAudioTranscriptionResult:
    """Outcome of extracting + transcribing a video's audio track (task 8.3)."""

    status: MediaRepresentationStatus
    segments: tuple[DerivedRepresentation, ...] = ()
    detail: str = ""


def _retime_locator_to_video(
    locator: TimeRangeLocator, video_resource_version_id: ResourceVersionId
) -> TimeRangeLocator:
    """Rebind a transcript locator to the video's resource, preserving timestamps.

    Audio is extracted from the video without trimming, so the extracted
    track's timeline is identical to the source video's timeline: only the
    ``resource_version_id`` needs to change to point at the original video
    (task 8.3: "reuse the audio transcription pipeline with original video
    time locators").
    """
    return locator.model_copy(update={"resource_version_id": video_resource_version_id})


def _retime_representation_to_video(
    representation: DerivedRepresentation, video_resource_version_id: ResourceVersionId
) -> DerivedRepresentation:
    retimed_locators = tuple(
        _retime_locator_to_video(loc, video_resource_version_id)
        if isinstance(loc, TimeRangeLocator)
        else loc
        for loc in representation.locators
    )
    return representation.model_copy(
        update={"resource_version_id": video_resource_version_id, "locators": retimed_locators}
    )


def transcribe_video_audio_track(
    video_metadata: VideoStreamInfo,
    video_resource_version_id: ResourceVersionId,
    *,
    audio_path: Path | None,
    source_content_hash: ContentHash,
    transcription_adapter: AudioTranscriptionAdapter | None,
    working_directory: Path,
) -> VideoAudioTranscriptionResult:
    """Transcribe an already-extracted local audio track for a video (task 8.3).

    Reuses the (Section 7) audio transcription pipeline via the structural
    :class:`AudioTranscriptionAdapter` Protocol and retimes every resulting
    transcript segment's locator to the original video's resource version.
    Silence/no audio track/no adapter configured all produce an explicit
    ``UNAVAILABLE`` result rather than fabricated text.
    """
    if not video_metadata.has_audio:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.UNAVAILABLE,
            detail="source video has no audio track",
        )
    if transcription_adapter is None:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.UNAVAILABLE,
            detail="no audio transcription adapter configured",
        )
    if audio_path is None:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.FAILED,
            detail="audio-track extraction produced no output",
        )

    raw_segments = transcription_adapter.transcribe(
        audio_path,
        resource_version_id=video_resource_version_id,
        source_content_hash=source_content_hash,
        working_directory=working_directory,
    )
    retimed = tuple(
        _retime_representation_to_video(seg, video_resource_version_id) for seg in raw_segments
    )
    if not retimed:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.PARTIAL,
            detail="transcription produced no segments (silence or unrecognized speech)",
        )
    all_complete = all(seg.coverage.is_complete for seg in retimed)
    status = (
        MediaRepresentationStatus.CURRENT if all_complete else MediaRepresentationStatus.PARTIAL
    )
    return VideoAudioTranscriptionResult(status=status, segments=retimed)


def transcribe_video_audio_track_via_audio_pipeline(
    video_metadata: VideoStreamInfo,
    video_resource_version_id: ResourceVersionId,
    *,
    audio_path: Path | None,
    source_content_hash: ContentHash,
    working_directory: Path,
    settings: MediaSamplingSettings,
) -> VideoAudioTranscriptionResult:
    """Real integration with Section 7's ``katsi_core.media.audio_pipeline``.

    RECONCILIATION: Section 7's ``AudioTranscriptionPipeline`` is a standard
    ``MediaPipelineProtocol`` adapter (``process(file_path, ...)`` returning
    one representation carrying the raw JSON transcription batch), not the
    ``AudioTranscriptionAdapter`` Protocol assumed above -- that Protocol
    documents the abstract shape this module needs, but the real pipeline
    is reused by running it through :class:`PipelineExecutionOrchestrator`
    like any other pipeline and then expanding the batch into per-segment
    representations with ``audio_pipeline.parse_transcript_segments`` /
    ``audio_pipeline.build_segment_representations``. Since those helpers
    accept ``resource_version_id`` directly, segments come out already
    bound to the video's resource version -- no separate retime step is
    needed here (unlike the generic :func:`transcribe_video_audio_track`
    path, which assumes an adapter that transcribes against its own audio
    resource id).

    Falls back to ``UNAVAILABLE`` (never fabricated text) if
    ``katsi_core.media.audio_pipeline`` cannot be imported, matching the
    Protocol path's behavior for a missing adapter.
    """
    if not video_metadata.has_audio:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.UNAVAILABLE,
            detail="source video has no audio track",
        )
    if audio_path is None:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.FAILED,
            detail="audio-track extraction produced no output",
        )

    try:
        from katsi_core.media.audio_pipeline import (
            AudioTranscriptionPipeline,
            build_segment_representations,
            parse_transcript_segments,
        )
    except ImportError:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.UNAVAILABLE,
            detail="katsi_core.media.audio_pipeline is not available",
        )

    adapter = AudioTranscriptionPipeline()
    definition = adapter.get_pipeline_definition()
    fingerprint = build_pipeline_fingerprint(
        source_content_hash=source_content_hash,
        representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        stage=PipelineStage.TRANSCRIBE,
        adapter_name=adapter.get_adapter_name(),
        adapter_version=adapter.get_adapter_version(),
        settings=settings,
    )
    batch_representation = PipelineExecutionOrchestrator().run(
        adapter,
        definition,
        audio_path,
        video_resource_version_id,
        source_content_hash,
        fingerprint,
    )
    if (
        batch_representation.status != MediaRepresentationStatus.CURRENT
        or not batch_representation.textual_payload
    ):
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.FAILED,
            detail="audio transcription batch failed or produced no output",
        )

    batch = json.loads(batch_representation.textual_payload)
    segments, coverage_fraction = parse_transcript_segments(batch)
    segment_representations = tuple(
        build_segment_representations(
            segments, video_resource_version_id, fingerprint, batch_representation.producer
        )
    )
    if not segment_representations:
        return VideoAudioTranscriptionResult(
            status=MediaRepresentationStatus.PARTIAL,
            detail="transcription produced no segments (silence or unrecognized speech)",
        )
    status = (
        MediaRepresentationStatus.CURRENT
        if coverage_fraction >= 1.0
        else MediaRepresentationStatus.PARTIAL
    )
    return VideoAudioTranscriptionResult(
        status=status,
        segments=segment_representations,
        detail=f"speech coverage fraction={coverage_fraction:.4f}",
    )


# =============================================================================
# 8.4 Scene-boundary detection with maximum-interval fallback sampling
# =============================================================================


def resolve_scene_boundaries(
    detected_boundaries_ms: Sequence[int], duration_ms: int, max_interval_ms: int
) -> tuple[int, ...]:
    """Merge detector output with a maximum-interval fallback (task 8.4).

    Guarantees the returned boundary sequence always starts at 0, ends at
    ``duration_ms``, is strictly increasing, and never has a gap larger
    than ``max_interval_ms`` -- so a scene detector that fails, returns
    nothing, or returns sparse boundaries always falls back to uniform
    sampling across the *entire* duration rather than leaving unscanned
    stretches or truncating to a prefix.
    """
    if duration_ms <= 0:
        return (0,)

    candidates = sorted({b for b in detected_boundaries_ms if 0 < b < duration_ms})

    boundaries: list[int] = [0]
    for candidate in candidates:
        while candidate - boundaries[-1] > max_interval_ms:
            boundaries.append(boundaries[-1] + max_interval_ms)
        if candidate > boundaries[-1]:
            boundaries.append(candidate)
    while duration_ms - boundaries[-1] > max_interval_ms:
        boundaries.append(boundaries[-1] + max_interval_ms)
    if boundaries[-1] != duration_ms:
        boundaries.append(duration_ms)

    return tuple(boundaries)


def build_scene_detection_definition(
    *, executable_path: str = "ffmpeg", pipeline_id: str = "video_scene_detect_v1"
) -> MediaPipelineDefinition:
    """Owner-registerable definition for ffmpeg scene-change detection."""
    return MediaPipelineDefinition(
        id=pipeline_id,
        name="Video scene-boundary detection (ffmpeg)",
        description="Detects scene-change timestamps via ffmpeg's scene filter.",
        stage=PipelineStage.DETECT_SCENES,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.SCENE],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-i",
            "{input_path}",
            "-filter:v",
            "select='gt(scene,0.4)',showinfo",
            "-f",
            "null",
            "-",
        ],
        network_disabled=True,
        timeout_seconds=120.0,
        max_output_bytes=5_000_000,
    )


_SHOWINFO_PTS_TIME_PATTERN = "pts_time:"


def parse_scene_boundaries_from_showinfo(text: str) -> tuple[int, ...]:
    """Extract scene-change timestamps (ms) from ffmpeg ``showinfo`` stderr output.

    Each detected scene-change frame logs a line containing
    ``pts_time:<seconds>``; this parses every occurrence, ignoring any line
    that does not match rather than raising, since partial/garbled detector
    output should degrade to fewer detected boundaries (filled by the
    maximum-interval fallback) rather than fail the whole stage.
    """
    boundaries: list[int] = []
    for line in text.splitlines():
        idx = line.find(_SHOWINFO_PTS_TIME_PATTERN)
        if idx == -1:
            continue
        rest = line[idx + len(_SHOWINFO_PTS_TIME_PATTERN) :]
        token = rest.split()[0] if rest.split() else ""
        try:
            boundaries.append(round(float(token) * 1000))
        except ValueError:
            continue
    return tuple(sorted(set(boundaries)))


class SceneDetectionPipeline(MediaPipelineProtocol):
    """Bounded scene-boundary detection adapter (task 8.4).

    Runs the owner-registered detector command; failures or empty results
    are surfaced as a low-coverage representation so callers apply the
    maximum-interval fallback via :func:`resolve_scene_boundaries` rather
    than treating detector failure as fatal.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        self._definition = definition or build_scene_detection_definition()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "video_scene_detect_ffmpeg"

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
        result = BoundedSubprocessExecutor().execute(self._definition, file_path, working_directory)
        now = datetime.now(UTC)
        producer = ProducerProvenance(
            producer_type=self._definition.producer_type,
            adapter_name=self.get_adapter_name(),
            adapter_version=self.get_adapter_version(),
        )
        rep_id = uuid4()

        # A non-zero exit or timeout is a detector *failure*: distinct from a
        # zero-boundary success (a video with no detected scene changes),
        # which still yields a CURRENT representation with an empty list --
        # the maximum-interval fallback handles both the same way downstream.
        if result.timed_out:
            return DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.SCENE,
                media_type="application/json",
                status=MediaRepresentationStatus.FAILED,
                created_at=now,
                updated_at=now,
                coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
                producer=producer,
                pipeline_fingerprint=pipeline_fingerprint,
                error=RepresentationError(
                    error_category="processing_error",
                    error_message="scene detection timed out",
                    is_retriable=True,
                ),
            )

        boundaries = parse_scene_boundaries_from_showinfo(result.stderr_sample)
        status = (
            MediaRepresentationStatus.CURRENT
            if result.exit_code == 0
            else MediaRepresentationStatus.PARTIAL
        )
        detail = (
            "scene boundaries detected"
            if result.exit_code == 0
            else "detector exited non-zero; boundaries (if any) are best-effort"
        )
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.SCENE,
            media_type="application/json",
            status=status,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps({"boundaries_ms": list(boundaries)}),
            coverage=MediaCoverage(
                is_complete=(status == MediaRepresentationStatus.CURRENT),
                coverage_fraction=1.0 if status == MediaRepresentationStatus.CURRENT else 0.0,
                detail=detail,
            ),
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, f"expected DerivedRepresentation, got {type(output).__name__}"
        if output.kind != representation_kind:
            return False, f"expected kind {representation_kind}, got {output.kind}"
        return True, None


# =============================================================================
# 8.5 Private keyframe extraction with frame/time locators + source-scene ties
# =============================================================================


@dataclass(frozen=True, slots=True)
class ScenePlan:
    """One planned scene: its time range and the single keyframe sampled from it."""

    start_ms: int
    end_ms: int
    keyframe_timestamp_ms: int


def plan_scenes_and_keyframes(
    boundaries_ms: Sequence[int], plan: VideoCoveragePlan
) -> tuple[ScenePlan, ...]:
    """Pair consecutive scene boundaries into scenes with one keyframe each.

    Scenes beyond ``plan.planned_keyframe_count`` are dropped rather than
    silently truncating the *time range* covered -- callers should treat a
    returned scene count below ``len(boundaries_ms) - 1`` as reduced
    keyframe density (already reflected in ``plan.coverage``), not a change
    in which portion of the video was scanned for scene boundaries.
    """
    if len(boundaries_ms) < 2:
        return ()

    scenes = [
        ScenePlan(
            start_ms=start,
            end_ms=end,
            keyframe_timestamp_ms=(start + end) // 2,
        )
        for start, end in zip(boundaries_ms, boundaries_ms[1:], strict=False)
    ]

    if plan.planned_keyframe_count <= 0 or len(scenes) <= plan.planned_keyframe_count:
        return tuple(scenes)

    # Spread the budgeted keyframe count evenly across the full scene list
    # rather than keeping only a leading prefix of scenes.
    step = len(scenes) / plan.planned_keyframe_count
    selected_indices = {
        min(len(scenes) - 1, round(i * step)) for i in range(plan.planned_keyframe_count)
    }
    return tuple(scenes[i] for i in sorted(selected_indices))


def build_keyframe_extraction_definition(
    *, executable_path: str = "ffmpeg", timestamp_ms: int, pipeline_id_suffix: str
) -> MediaPipelineDefinition:
    """Owner/core-constructed definition for one keyframe extraction at a fixed timestamp.

    The timestamp is baked into the fixed argument template by core code
    (never agent-supplied), consistent with ``ALLOWED_ARG_PLACEHOLDERS`` only
    covering ``input_path``/``output_path``/``working_directory``.
    """
    timestamp_s = timestamp_ms / 1000.0
    return MediaPipelineDefinition(
        id=f"video_keyframe_extract_{pipeline_id_suffix}",
        name="Video keyframe extraction (ffmpeg)",
        description="Extracts a single decoded frame at a fixed timestamp.",
        stage=PipelineStage.EXTRACT_KEYFRAMES,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.KEYFRAME],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=[
            "-y",
            "-ss",
            f"{timestamp_s:.3f}",
            "-i",
            "{input_path}",
            "-frames:v",
            "1",
            "{output_path}",
        ],
        network_disabled=True,
        timeout_seconds=30.0,
        max_output_bytes=20_000_000,
    )


class KeyframeExtractionPipeline(MediaPipelineProtocol):
    """Extracts one private keyframe blob at a fixed timestamp (task 8.5).

    One instance is constructed per planned keyframe timestamp (mirroring
    the pattern of per-call, core-constructed ``MediaPipelineDefinition``
    values -- see :func:`build_keyframe_extraction_definition`).
    """

    def __init__(
        self,
        *,
        timestamp_ms: int,
        frame_index: int | None = None,
        source_scene: ScenePlan | None = None,
        definition: MediaPipelineDefinition | None = None,
        blob_store: BlobStore | None = None,
    ) -> None:
        self._timestamp_ms = timestamp_ms
        self._frame_index = frame_index
        self._source_scene = source_scene
        self._definition = definition or build_keyframe_extraction_definition(
            timestamp_ms=timestamp_ms, pipeline_id_suffix=str(timestamp_ms)
        )
        self._blob_store = blob_store

    @classmethod
    def get_adapter_name(cls) -> str:
        return "video_keyframe_extract_ffmpeg"

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
        output_path = working_directory / f"keyframe_{self._timestamp_ms}.jpg"
        result = BoundedSubprocessExecutor().execute(
            self._definition, file_path, working_directory, output_path=output_path
        )
        now = datetime.now(UTC)
        producer = ProducerProvenance(
            producer_type=self._definition.producer_type,
            adapter_name=self.get_adapter_name(),
            adapter_version=self.get_adapter_version(),
        )
        rep_id = uuid4()

        if result.exit_code != 0 or result.timed_out or not output_path.exists():
            return DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.KEYFRAME,
                media_type="image/jpeg",
                status=MediaRepresentationStatus.FAILED,
                created_at=now,
                updated_at=now,
                blob_reference="unavailable",
                blob_hash="0" * 32,
                blob_byte_count=0,
                coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
                producer=producer,
                pipeline_fingerprint=pipeline_fingerprint,
                error=RepresentationError(
                    error_category="processing_error",
                    error_message=result.stderr_sample or "keyframe extraction failed",
                    is_retriable=not result.timed_out,
                ),
            )

        if self._blob_store is None:
            raise RuntimeError("KeyframeExtractionPipeline requires a blob_store to persist output")

        image_bytes = output_path.read_bytes()
        blob_hash, byte_count = self._blob_store.store_blob(image_bytes)
        locators: tuple[Any, ...] = (
            VideoFrameLocator(
                resource_version_id=resource_version_id,
                representation_id=rep_id,
                timestamp_ms=self._timestamp_ms,
                frame_index=self._frame_index,
            ),
        )
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.KEYFRAME,
            media_type="image/jpeg",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            blob_reference=f"blob:{blob_hash}",
            blob_hash=blob_hash,
            blob_byte_count=byte_count,
            locators=locators,
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, f"expected DerivedRepresentation, got {type(output).__name__}"
        if output.kind != representation_kind:
            return False, f"expected kind {representation_kind}, got {output.kind}"
        if output.status == MediaRepresentationStatus.CURRENT and (
            output.blob_reference is None or output.blob_hash is None
        ):
            return False, "current keyframe representation must carry a blob reference and hash"
        return True, None


# =============================================================================
# 8.6 Optional keyframe captions and visual embeddings (image pipeline reuse)
# =============================================================================


@runtime_checkable
class ImageCaptionAdapter(Protocol):
    """Structural shape expected from Section 5's image captioning pipeline.

    RECONCILIATION NOTE: assumed shape; adapt the call site in
    :func:`caption_keyframe` if Section 5's real API differs.
    """

    def caption(
        self,
        image_bytes: bytes,
        *,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        working_directory: Path,
    ) -> DerivedRepresentation: ...


@runtime_checkable
class ImageEmbeddingAdapter(Protocol):
    """Structural shape expected from Section 5's visual embedding pipeline.

    RECONCILIATION NOTE: assumed shape; adapt the call site in
    :func:`embed_keyframe` if Section 5's real API differs.
    """

    def embed(
        self,
        image_bytes: bytes,
        *,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        working_directory: Path,
    ) -> DerivedRepresentation: ...


def caption_keyframe(
    adapter: ImageCaptionAdapter | None,
    keyframe: DerivedRepresentation,
    image_bytes: bytes,
    *,
    source_content_hash: ContentHash,
    working_directory: Path,
) -> DerivedRepresentation | None:
    """Optionally caption a keyframe through the image pipeline (task 8.6).

    Returns ``None`` (never a fabricated caption) when no adapter is
    configured or when captioning raises -- captions are optional, and
    their absence must not fail keyframe extraction itself.
    """
    if adapter is None:
        return None
    try:
        return adapter.caption(
            image_bytes,
            resource_version_id=keyframe.resource_version_id,
            source_content_hash=source_content_hash,
            working_directory=working_directory,
        )
    except Exception:  # noqa: BLE001 -- optional enrichment, never fatal
        return None


def embed_keyframe(
    adapter: ImageEmbeddingAdapter | None,
    keyframe: DerivedRepresentation,
    image_bytes: bytes,
    *,
    source_content_hash: ContentHash,
    working_directory: Path,
) -> DerivedRepresentation | None:
    """Optionally embed a keyframe through the image pipeline (task 8.6).

    Same optional-enrichment contract as :func:`caption_keyframe`.
    """
    if adapter is None:
        return None
    try:
        return adapter.embed(
            image_bytes,
            resource_version_id=keyframe.resource_version_id,
            source_content_hash=source_content_hash,
            working_directory=working_directory,
        )
    except Exception:  # noqa: BLE001 -- optional enrichment, never fatal
        return None


def _run_image_pipeline_adapter(
    adapter: MediaPipelineProtocol,
    image_path: Path,
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    fingerprint: PipelineFingerprint,
) -> DerivedRepresentation | None:
    try:
        representation = PipelineExecutionOrchestrator().run(
            adapter,
            adapter.get_pipeline_definition(),
            image_path,
            resource_version_id,
            source_content_hash,
            fingerprint,
        )
    except Exception:  # noqa: BLE001 -- optional enrichment, never fatal
        return None
    if representation.status != MediaRepresentationStatus.CURRENT:
        return None
    return representation


def caption_keyframe_via_image_pipeline(
    keyframe: DerivedRepresentation,
    image_path: Path,
    *,
    source_content_hash: ContentHash,
    settings: MediaSamplingSettings,
) -> DerivedRepresentation | None:
    """Real integration with Section 5's ``katsi_core.media.image_pipeline`` (task 8.6).

    RECONCILIATION: Section 5's ``ImageCaptionPipeline``/
    ``ImageVisualEmbeddingPipeline`` are standard ``MediaPipelineProtocol``
    adapters (``process(file_path, ...)``), not the bytes-based
    ``ImageCaptionAdapter``/``ImageEmbeddingAdapter`` Protocols declared
    above -- those Protocols document the abstract shape; this function
    reuses the real pipeline via :class:`PipelineExecutionOrchestrator`,
    which requires the keyframe as a file on disk (write
    ``blob_store.get_blob(keyframe.blob_hash)`` out to a temp file first).
    Returns ``None`` on any failure or if the module is unavailable --
    captions are optional and must never block keyframe extraction.
    """
    try:
        from katsi_core.media.image_pipeline import ImageCaptionPipeline
    except ImportError:
        return None

    adapter = ImageCaptionPipeline()
    fingerprint = build_pipeline_fingerprint(
        source_content_hash=source_content_hash,
        representation_kind=MediaRepresentationKind.IMAGE_CAPTION,
        stage=PipelineStage.CAPTION,
        adapter_name=adapter.get_adapter_name(),
        adapter_version=adapter.get_adapter_version(),
        settings=settings,
    )
    return _run_image_pipeline_adapter(
        adapter, image_path, keyframe.resource_version_id, source_content_hash, fingerprint
    )


def embed_keyframe_via_image_pipeline(
    keyframe: DerivedRepresentation,
    image_path: Path,
    *,
    source_content_hash: ContentHash,
    settings: MediaSamplingSettings,
) -> DerivedRepresentation | None:
    """Real integration with Section 5's visual embedding pipeline (task 8.6).

    See :func:`caption_keyframe_via_image_pipeline` for the reconciliation
    note on why this bypasses the ``ImageEmbeddingAdapter`` Protocol above.
    """
    try:
        from katsi_core.media.image_pipeline import ImageVisualEmbeddingPipeline
    except ImportError:
        return None

    adapter = ImageVisualEmbeddingPipeline()
    fingerprint = build_pipeline_fingerprint(
        source_content_hash=source_content_hash,
        representation_kind=MediaRepresentationKind.VISUAL_EMBEDDING,
        stage=PipelineStage.EMBED_VISUAL,
        adapter_name=adapter.get_adapter_name(),
        adapter_version=adapter.get_adapter_version(),
        settings=settings,
    )
    return _run_image_pipeline_adapter(
        adapter, image_path, keyframe.resource_version_id, source_content_hash, fingerprint
    )


# =============================================================================
# 8.7 Scene representations: range + keyframes + overlapping transcript evidence
# =============================================================================


def _segments_overlap_scene(segment: DerivedRepresentation, start_ms: int, end_ms: int) -> bool:
    for locator in segment.locators:
        if not isinstance(locator, TimeRangeLocator):
            continue
        if locator.start_ms < end_ms and locator.end_ms > start_ms:
            return True
    return False


def build_scene_representations(
    scenes: Sequence[ScenePlan],
    keyframes_by_timestamp: dict[int, DerivedRepresentation],
    transcript_segments: Sequence[DerivedRepresentation],
    *,
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    settings: MediaSamplingSettings,
    adapter_name: str = "video_scene_composer",
    adapter_version: str = "1.0.0",
) -> tuple[DerivedRepresentation, ...]:
    """Compose final SCENE representations (task 8.7).

    Pure, in-process composition -- no subprocess is involved, since every
    input (scene range, selected keyframes, transcript segments) was
    already produced by upstream bounded stages. Combines:

    - the scene's time range (``SceneLocator.start_ms``/``end_ms``);
    - its selected keyframe id(s) (``SceneLocator.keyframe_ids``);
    - any transcript segments whose time range overlaps the scene, joined
      into ``textual_payload`` as retrievable evidence.

    A scene whose keyframe extraction failed (missing from
    ``keyframes_by_timestamp``) is still emitted, but with reduced
    ``MediaCoverage`` -- never silently dropped and never reported complete.
    """
    representations: list[DerivedRepresentation] = []
    now = datetime.now(UTC)
    producer = ProducerProvenance(
        producer_type=MediaProducerType.DETERMINISTIC,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
    )

    for scene in scenes:
        keyframe = keyframes_by_timestamp.get(scene.keyframe_timestamp_ms)
        keyframe_ids = (keyframe.id,) if keyframe is not None else ()

        overlapping = [
            seg
            for seg in transcript_segments
            if _segments_overlap_scene(seg, scene.start_ms, scene.end_ms)
        ]
        textual_payload = (
            "\n".join(seg.textual_payload for seg in overlapping if seg.textual_payload) or None
        )

        rep_id = uuid4()
        locator = SceneLocator(
            resource_version_id=resource_version_id,
            representation_id=rep_id,
            start_ms=scene.start_ms,
            end_ms=scene.end_ms,
            keyframe_ids=keyframe_ids,
        )

        has_keyframe = keyframe is not None and keyframe.status == MediaRepresentationStatus.CURRENT
        coverage = (
            MediaCoverage(is_complete=True, coverage_fraction=1.0, detail="keyframe present")
            if has_keyframe
            else MediaCoverage(
                is_complete=False,
                coverage_fraction=0.0,
                detail="no keyframe available for this scene",
            )
        )

        fingerprint = build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.SCENE,
            stage=PipelineStage.DETECT_SCENES,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            settings=settings,
        )

        representations.append(
            DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.SCENE,
                media_type="application/json",
                status=(
                    MediaRepresentationStatus.CURRENT
                    if has_keyframe
                    else MediaRepresentationStatus.PARTIAL
                ),
                created_at=now,
                updated_at=now,
                textual_payload=textual_payload,
                locators=(locator,),
                coverage=coverage,
                producer=producer,
                pipeline_fingerprint=fingerprint,
            )
        )

    return tuple(representations)


__all__ = [
    "AudioTranscriptionAdapter",
    "ImageCaptionAdapter",
    "ImageEmbeddingAdapter",
    "KeyframeExtractionPipeline",
    "ScenePlan",
    "SceneDetectionPipeline",
    "VideoAudioExtractionPipeline",
    "VideoAudioTranscriptionResult",
    "VideoComputeClass",
    "VideoCoverageBudget",
    "VideoCoveragePlan",
    "VideoCoveragePlanner",
    "VideoCoveragePolicy",
    "VideoMetadataPipeline",
    "VideoStreamInfo",
    "build_audio_track_extraction_definition",
    "build_keyframe_extraction_definition",
    "build_scene_detection_definition",
    "build_scene_representations",
    "build_video_metadata_definition",
    "caption_keyframe",
    "caption_keyframe_via_image_pipeline",
    "embed_keyframe",
    "embed_keyframe_via_image_pipeline",
    "parse_scene_boundaries_from_showinfo",
    "parse_video_stream_metadata",
    "plan_scenes_and_keyframes",
    "resolve_scene_boundaries",
    "transcribe_video_audio_track",
    "transcribe_video_audio_track_via_audio_pipeline",
]
