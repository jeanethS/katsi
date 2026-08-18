"""Strict contracts for multimedia understanding integration.

This module defines the core integration contracts that reconcile multimedia
representations with the agentic-workspace-coordination system. All models are
immutable Pydantic contracts with strict validation and JSON serialization.

Multimedia representations extend the ResourceVersion/Claim system without
breaking existing text-only workflows. Derived representations attach to
immutable ResourceVersions, and Claims can cite representation-specific
evidence through typed EvidenceLocators.

Key integration points:
- ResourceVersion: Source of truth for original content hash and bytes
- ClaimEvidence.reference: Can store representation_id + locator evidence
- Existing Chunk/Extraction: Compatible with new Representation model via conversion
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from katsi_core.config import MediaSamplingSettings
from katsi_core.workspace.contracts import (
    ContentHash,
    ImmutableModel,
    ResourceVersionId,
    StrictModel,
)

# =============================================================================
# Strict Enums for Media Types and Statuses
# =============================================================================


class MediaRepresentationKind(StrEnum):
    """Complete enumeration of derived representation kinds."""

    # Metadata and foundational representations
    METADATA = "metadata"
    MEDIA_DESCRIPTOR = "media_descriptor"

    # Text-based representations
    EXTRACTED_TEXT = "extracted_text"
    OCR_TEXT = "ocr_text"
    IMAGE_CAPTION = "image_caption"
    TRANSCRIPT_SEGMENT = "transcript_segment"

    # Visual representations
    THUMBNAIL = "thumbnail"
    KEYFRAME = "keyframe"
    SCENE = "scene"

    # Semantic representations
    VISUAL_EMBEDDING = "visual_embedding"
    TEXT_EMBEDDING = "text_embedding"

    # Derived artifacts
    PROXY_MEDIA = "proxy_media"


class MediaRepresentationStatus(StrEnum):
    """Independent lifecycle status for each representation."""

    PENDING = "pending"  # Queued or in progress
    CURRENT = "current"  # Latest successful version
    PARTIAL = "partial"  # Succeeded with incomplete coverage
    UNAVAILABLE = "unavailable"  # Cannot be produced (encrypted, unsupported, etc.)
    FAILED = "failed"  # Attempted but errored


class MediaTypeFamily(StrEnum):
    """High-level media type families for pipeline selection."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    TEXT = "text"
    UNKNOWN = "unknown"


class MediaPrivacyClass(StrEnum):
    """Classification for sensitive metadata requiring capability grants."""

    NONE = "none"  # No sensitive content
    LOCATION = "location"  # GPS coordinates, EXIF location, etc.
    BIOMETRIC_LIKE = "biometric_like"  # Face regions, voice segments, etc.
    PERSONAL = "personal"  # Names, addresses, identifiers in content


class MediaProducerType(StrEnum):
    """Category of representation producer."""

    DETERMINISTIC = "deterministic"  # Pure function, no model
    MODEL_BACKED = "model_backed"  # Uses a ML model
    HYBRID = "hybrid"  # Combination of deterministic and model


class PipelineStage(StrEnum):
    """Named stages in the media processing DAG."""

    DETECT = "detect"
    EXTRACT_METADATA = "extract_metadata"
    EXTRACT_TEXT = "extract_text"
    OCR = "ocr"
    CAPTION = "caption"
    TRANSCRIBE = "transcribe"
    DETECT_SCENES = "detect_scenes"
    EXTRACT_KEYFRAMES = "extract_keyframes"
    GENERATE_THUMBNAIL = "generate_thumbnail"
    GENERATE_PROXY = "generate_proxy"
    EMBED_VISUAL = "embed_visual"
    EMBED_TEXT = "embed_text"
    SEGMENT_SPEAKERS = "segment_speakers"


class EmbeddingSpaceFingerprint(StrEnum):
    """Known embedding space identifiers for visual/text embeddings."""

    # CLIP-like spaces
    CLIP_VIT_B_32 = "clip_vit_b_32"
    CLIP_VIT_B_16 = "clip_vit_b_16"
    CLIP_VIT_L_14 = "clip_vit_l_14"

    # Other vision encoders
    SIGLIP_B_16 = "siglip_b_16"
    SIGLIP_L_16 = "siglip_l_16"

    # Text embeddings
    BGE_M3 = "bge_m3"
    E5_LARGE = "e5_large"
    COHERE_EMBED_V3 = "cohere_embed_v3"

    # Generic fallback
    UNKNOWN_SPACE = "unknown_space"


# =============================================================================
# Evidence Locators - Discriminated Union for Spatial/Temporal References
# =============================================================================


class EvidenceLocator(ImmutableModel):
    """Base class for all evidence locators.

    Every locator carries the immutable source resource-version id to ensure
    that evidence references can never become detached from their source.

    Locators use normalized coordinates where possible (0-1 for regions,
    milliseconds for time) to work across different resolutions and formats.
    """

    resource_version_id: ResourceVersionId
    representation_id: UUID = Field(
        description="UUID of the representation containing this evidence"
    )


class WholeResourceLocator(EvidenceLocator):
    """Locator for evidence derived from the entire resource."""

    locator_type: Literal["whole_resource"] = "whole_resource"


class TextRangeLocator(EvidenceLocator):
    """Locator for text ranges within a text-based representation.

    Uses character offsets for stability across encoding changes.
    """

    locator_type: Literal["text_range"] = "text_range"
    start_char: int = Field(ge=0, description="Inclusive start offset in characters")
    end_char: int = Field(ge=0, description="Exclusive end offset in characters")

    @model_validator(mode="after")
    def validate_range(self) -> TextRangeLocator:
        if self.end_char <= self.start_char:
            raise ValueError("end_char must be greater than start_char")
        return self


class PageLocator(EvidenceLocator):
    """Locator for document pages with optional region.

    Uses one-based page numbers for user-facing clarity and normalized
    bounding boxes for resolution independence.
    """

    locator_type: Literal["page"] = "page"
    page_number: int = Field(ge=1, description="One-based page number")
    bounding_box: tuple[float, float, float, float] | None = (
        None  # [x, y, width, height] normalized 0-1
    )

    @model_validator(mode="after")
    def validate_bounding_box(self) -> PageLocator:
        if self.bounding_box is not None:
            x, y, w, h = self.bounding_box
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= w <= 1 and 0 <= h <= 1):
                raise ValueError("Bounding box coordinates must be normalized to [0, 1]")
        return self


class ImageRegionLocator(EvidenceLocator):
    """Locator for regions within an image.

    Uses normalized coordinates [0, 1] for resolution independence.
    """

    locator_type: Literal["image_region"] = "image_region"
    bounding_box: tuple[float, float, float, float] = Field(
        description="[x, y, width, height] normalized 0-1"
    )

    @model_validator(mode="after")
    def validate_bounding_box(self) -> ImageRegionLocator:
        x, y, w, h = self.bounding_box
        if not (0 <= x <= 1 and 0 <= y <= 1 and w > 0 and h > 0 and x + w <= 1 and y + h <= 1):
            raise ValueError(
                "Bounding box must be normalized, positive width/height, and within bounds"
            )
        return self

    def __hash__(self) -> int:
        """Make ImageRegionLocator hashable."""
        return hash(
            (self.resource_version_id, self.representation_id, self.locator_type, self.bounding_box)
        )


class TimeRangeLocator(EvidenceLocator):
    """Locator for time ranges in audio/video.

    Uses integer milliseconds for precision and float equality stability.
    """

    locator_type: Literal["time_range"] = "time_range"
    start_ms: int = Field(ge=0, description="Start time in milliseconds")
    end_ms: int = Field(ge=0, description="End time in milliseconds (exclusive)")

    @model_validator(mode="after")
    def validate_range(self) -> TimeRangeLocator:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class VideoFrameLocator(EvidenceLocator):
    """Locator for specific video frames.

    Combines timestamp with optional frame index for precision.
    """

    locator_type: Literal["video_frame"] = "video_frame"
    timestamp_ms: int = Field(ge=0, description="Frame timestamp in milliseconds")
    frame_index: int | None = Field(
        default=None, ge=0, description="Decoded frame index if available"
    )


class SceneLocator(EvidenceLocator):
    """Locator for video scenes combining time range and keyframes.

    Scenes can span multiple keyframes and overlap with transcript segments.
    """

    locator_type: Literal["scene"] = "scene"
    start_ms: int = Field(ge=0, description="Scene start in milliseconds")
    end_ms: int = Field(ge=0, description="Scene end in milliseconds")
    keyframe_ids: tuple[UUID, ...] = Field(
        default_factory=tuple, description="Selected keyframe IDs in this scene"
    )

    @model_validator(mode="after")
    def validate_range(self) -> SceneLocator:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


# Discriminated union for all locator types
EvidenceLocatorUnion = Annotated[
    WholeResourceLocator
    | TextRangeLocator
    | PageLocator
    | ImageRegionLocator
    | TimeRangeLocator
    | VideoFrameLocator
    | SceneLocator,
    Field(discriminator="locator_type"),
]


# =============================================================================
# Media Descriptors and Metadata
# =============================================================================


class MediaDescriptor(StrictModel):
    """Deterministic media metadata from content inspection.

    Produced by the detect stage, this descriptor contains MIME type,
    dimensions, duration, and structural metadata without executing any
    embedded content.
    """

    mime_type: str = Field(min_length=1, description="MIME type from content inspection")
    extension_hint: str | None = Field(default=None, description="Original file extension as hint")
    family: MediaTypeFamily = Field(description="High-level media family")

    # Image/video dimensions
    width: int | None = Field(default=None, ge=1, description="Width in pixels")
    height: int | None = Field(default=None, ge=1, description="Height in pixels")

    # Audio/video duration
    duration_ms: int | None = Field(default=None, ge=0, description="Duration in milliseconds")

    # Document-specific
    page_count: int | None = Field(default=None, ge=1, description="Page count for documents")

    # Codec information
    codec: str | None = Field(default=None, description="Codec name if applicable")
    container: str | None = Field(default=None, description="Container format")

    # Warnings
    extension_mismatch: bool = Field(
        default=False, description="True if extension doesn't match content"
    )
    encrypted: bool = Field(default=False, description="True if content appears encrypted")
    password_protected: bool = Field(default=False, description="True if requires password")
    malformed: bool = Field(default=False, description="True if content appears malformed")


class MediaCoverage(StrictModel):
    """Description of partial vs. complete representation coverage."""

    is_complete: bool = Field(description="True if representation covers entire source")
    coverage_fraction: float = Field(ge=0.0, le=1.0, description="Estimated coverage fraction")
    detail: str = Field(default="", description="Human-readable coverage explanation")

    @field_validator("coverage_fraction")
    @classmethod
    def validate_coverage_fraction(cls, v: float) -> float:
        """Ensure coverage fraction is in valid range."""
        if not (0.0 <= v <= 1.0):
            raise ValueError("coverage_fraction must be between 0.0 and 1.0")
        return v

    @model_validator(mode="after")
    def validate_completeness(self) -> MediaCoverage:
        if self.is_complete and self.coverage_fraction != 1.0:
            raise ValueError("is_complete=True requires coverage_fraction=1.0")
        return self


class ProducerProvenance(StrictModel):
    """Information about the producer of a representation."""

    producer_type: MediaProducerType = Field(description="Category of producer")
    adapter_name: str = Field(min_length=1, description="Adapter or tool name")
    adapter_version: str = Field(min_length=1, description="Version string")
    model_identity: str | None = Field(
        default=None, description="Model name/tool identifier if applicable"
    )
    model_version: str | None = Field(default=None, description="Model version if applicable")

    def get_fingerprint_components(self) -> dict[str, str | None]:
        """Extract components for pipeline fingerprint computation."""
        return {
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "model_identity": self.model_identity,
            "model_version": self.model_version,
        }


class PipelineFingerprint(ImmutableModel):
    """Complete fingerprint for representation cache keys.

    This fingerprint contains all inputs that affect the output of a media
    pipeline stage. Identical fingerprints should produce identical outputs
    (modulo non-deterministic model behavior).
    """

    source_content_hash: ContentHash = Field(description="Hash of original source bytes")
    input_representation_id: UUID | None = Field(
        default=None, description="Input representation for downstream stages"
    )
    representation_kind: MediaRepresentationKind = Field(
        description="Kind of representation produced"
    )
    stage: PipelineStage = Field(description="Pipeline stage that produced this")

    # Producer identity
    adapter_name: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    model_identity: str | None = Field(default=None)
    model_version: str | None = Field(default=None)

    # Policy and configuration
    sampling_fingerprint: str = Field(
        min_length=1, description="Hash of sampling/chunking configuration"
    )
    language_policy: str = Field(default="*", description="Language code or wildcard")
    ocr_language: str | None = Field(default=None, description="OCR language if applicable")
    prompt_version: str | None = Field(
        default=None, description="Semantic prompt version if applicable"
    )
    normalization_version: str = Field(default="v1", description="Output normalization version")

    def get_cache_key_components(self) -> dict[str, str | int]:
        """Extract all components for cache key computation."""
        model_key = "none"
        if self.model_identity:
            model_ver = self.model_version or "unknown"
            model_key = f"{self.model_identity}@{model_ver}"

        return {
            "source_hash": self.source_content_hash,
            "input_rep": str(self.input_representation_id)
            if self.input_representation_id
            else "none",
            "kind": self.representation_kind.value,
            "stage": self.stage.value,
            "adapter": f"{self.adapter_name}@{self.adapter_version}",
            "model": model_key,
            "sampling": self.sampling_fingerprint,
            "language": self.language_policy,
            "ocr_lang": self.ocr_language or "none",
            "prompt": self.prompt_version or "none",
            "norm": self.normalization_version,
        }


def compute_sampling_fingerprint(settings: MediaSamplingSettings) -> str:
    """Derive `PipelineFingerprint.sampling_fingerprint` from a chunking policy.

    Deterministically hashes the sampling/chunking configuration so that any
    change to `target_tokens`, `overlap`, or `separator_hierarchy` (Decision 16:
    chunking policy versioning) produces a distinct fingerprint value. Stages
    that use `MediaSamplingSettings` should call this instead of hardcoding a
    literal string, so cache lookups correctly treat differently-chunked
    representations as incompatible.
    """
    import hashlib
    import json

    components = settings.get_fingerprint_components()
    canonical = json.dumps(components, sort_keys=True, default=list)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RepresentationError(StrictModel):
    """Structured error information for failed representations."""

    error_category: str = Field(min_length=1, description="Category of error")
    error_message: str = Field(min_length=1, description="Human-readable error message")
    is_retriable: bool = Field(default=False, description="True if error might be transient")
    diagnostic_info: dict[str, str] = Field(
        default_factory=dict, description="Additional diagnostic context"
    )


# =============================================================================
# Derived Representation Core Model
# =============================================================================


class DerivedRepresentation(ImmutableModel):
    """A derived representation attached to an immutable ResourceVersion.

    Representations are immutable and independently versioned. Each has its
    own lifecycle status, confidence metadata, and coverage information.

    Textual payloads are stored inline; large binaries use blob references
    to avoid unbounded memory/disk usage.
    """

    id: UUID = Field(description="Unique representation identifier")
    resource_version_id: ResourceVersionId = Field(description="Source immutable resource version")
    kind: MediaRepresentationKind = Field(description="Kind of representation")
    media_type: str = Field(min_length=1, description="MIME type of representation content")

    # Lifecycle
    status: MediaRepresentationStatus = Field(description="Current lifecycle status")
    created_at: datetime = Field(description="When this representation was created")
    updated_at: datetime = Field(description="When this representation was last updated")

    # Content
    textual_payload: str | None = Field(
        default=None, description="Text content for text-based representations"
    )
    blob_reference: str | None = Field(
        default=None, description="Reference to private blob for binary content"
    )
    blob_hash: ContentHash | None = Field(
        default=None, description="Hash of blob content if applicable"
    )
    blob_byte_count: int | None = Field(default=None, ge=0, description="Size of blob in bytes")

    # Evidence and quality
    locators: tuple[EvidenceLocatorUnion, ...] = Field(
        default_factory=tuple, description="Evidence locators"
    )
    coverage: MediaCoverage = Field(
        default_factory=lambda: MediaCoverage(is_complete=True, coverage_fraction=1.0)
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Confidence score where applicable"
    )
    privacy_classes: frozenset[MediaPrivacyClass] = Field(
        default_factory=frozenset,
        description="Sensitive classifications carried by this representation",
    )

    # Provenance
    producer: ProducerProvenance = Field(description="Information about the producer")
    pipeline_fingerprint: PipelineFingerprint = Field(description="Complete pipeline fingerprint")

    # Error information for failed/unavailable representations
    error: RepresentationError | None = Field(
        default=None, description="Error details if status is FAILED or UNAVAILABLE"
    )

    @model_validator(mode="after")
    def validate_content(self) -> DerivedRepresentation:
        """Ensure representation has appropriate content for its kind."""
        text_kinds = {
            MediaRepresentationKind.EXTRACTED_TEXT,
            MediaRepresentationKind.OCR_TEXT,
            MediaRepresentationKind.IMAGE_CAPTION,
            MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        }

        if self.kind in text_kinds and self.textual_payload is None:
            raise ValueError(f"{self.kind} representations require textual_payload")

        if self.kind in text_kinds and self.blob_reference is not None:
            raise ValueError(f"{self.kind} representations should not have blob_reference")

        if self.kind in {
            MediaRepresentationKind.THUMBNAIL,
            MediaRepresentationKind.KEYFRAME,
            MediaRepresentationKind.PROXY_MEDIA,
        } and (self.blob_reference is None or self.blob_hash is None):
            raise ValueError(f"{self.kind} representations require blob_reference and blob_hash")

        return self

    @model_validator(mode="after")
    def validate_error_status(self) -> DerivedRepresentation:
        """Ensure failed/unavailable representations have error information."""
        if (
            self.status in {MediaRepresentationStatus.FAILED, MediaRepresentationStatus.UNAVAILABLE}
            and self.error is None
        ):
            raise ValueError(f"{self.status} representations must include error information")
        return self


# =============================================================================
# Configuration Models for Media Processing
# =============================================================================


class MediaMimePattern(BaseModel):
    """MIME pattern configuration for media type detection."""

    pattern: str = Field(min_length=1, description="Glob pattern for MIME types")
    enabled: bool = Field(default=True, description="Whether this pattern is enabled")
    required_pipeline: str | None = Field(
        default=None, description="Required pipeline ID for this pattern"
    )


class MediaPipelineDefinition(BaseModel):
    """Definition of a media processing pipeline.

    Pipelines are configured by owners and selected by agents. They declare
    their inputs, outputs, resource budgets, and execution policies.
    """

    id: str = Field(min_length=1, description="Pipeline identifier")
    name: str = Field(min_length=1, max_length=256, description="Human-readable name")
    description: str = Field(
        default="", max_length=2000, description="Purpose and behavior description"
    )

    # Input/output contract
    stage: PipelineStage = Field(description="Pipeline stage this definition implements")
    accepted_mime_patterns: list[str] = Field(
        default_factory=list, description="Accepted MIME type globs"
    )
    input_kinds: list[MediaRepresentationKind] = Field(
        default_factory=list,
        description=(
            "Representation kinds this pipeline consumes as input. Empty means "
            "this is a root stage that consumes the raw source (e.g. detection, "
            "metadata extraction) rather than an upstream representation."
        ),
    )
    representation_kinds_produced: list[MediaRepresentationKind] = Field(
        default_factory=list,
        min_length=1,
        description="Kinds of representations this pipeline produces",
    )

    # Producer identity
    producer_type: MediaProducerType = Field(description="Category of producer")
    executable_path: str | None = Field(
        default=None, description="Path to executable for deterministic pipelines"
    )
    model_identity: str | None = Field(
        default=None, description="Model identifier for model-backed pipelines"
    )

    # Execution policy
    fixed_args: list[str] = Field(
        default_factory=list,
        description=(
            "Fixed argument template. Elements may reference exactly the "
            "placeholders in katsi_core.media.execution.ALLOWED_ARG_PLACEHOLDERS "
            "(input_path, output_path, working_directory) as {placeholder} "
            "tokens; any other placeholder is rejected at execution time. "
            "This set is fixed and not owner-extensible."
        ),
    )
    allowed_env_vars: list[str] = Field(
        default_factory=list, description="Allowed environment variables"
    )
    working_directory: str = Field(default=".", description="Working directory for execution")
    shell_enabled: bool = Field(
        default=False, description="Whether shell is enabled (should be False)"
    )
    network_disabled: bool = Field(default=True, description="Whether network access is disabled")

    # Resource budgets
    timeout_seconds: float = Field(default=60.0, gt=0, description="Execution timeout")
    max_memory_mb: int | None = Field(default=None, ge=0, description="Maximum memory in MB")
    max_output_bytes: int = Field(
        default=10_000_000, gt=0, description="Maximum output size in bytes"
    )
    max_duration_ms: int | None = Field(
        default=None, ge=0, description="Maximum media duration to process"
    )
    max_pages: int | None = Field(default=None, ge=0, description="Maximum pages for documents")
    max_keyframes: int | None = Field(default=None, ge=0, description="Maximum keyframes for video")

    # Validation
    strict_output_contract: bool = Field(
        default=True, description="Whether output is strictly validated"
    )
    retry_on_failure: bool = Field(default=True, description="Whether to retry once on failure")

    # Availability
    availability_probe: str | None = Field(
        default=None, description="Command to check availability"
    )
    required_hardware: list[str] = Field(
        default_factory=list, description="Required hardware features"
    )


class MediaProcessingConfig(BaseModel):
    """Complete media processing configuration.

    This configuration controls which media types are supported, which
    pipelines are available, and what resource limits apply.
    """

    # MIME type support
    enabled_mime_patterns: list[MediaMimePattern] = Field(
        default_factory=list, description="Enabled MIME type patterns"
    )

    # Pipeline catalog
    pipelines: list[MediaPipelineDefinition] = Field(
        default_factory=list, description="Available processing pipelines"
    )

    # Model and tool identities
    default_ocr_pipeline: str | None = Field(default=None, description="Default OCR pipeline ID")
    default_caption_pipeline: str | None = Field(
        default=None, description="Default caption pipeline ID"
    )
    default_transcription_pipeline: str | None = Field(
        default=None, description="Default transcription pipeline ID"
    )
    default_embedding_pipeline: str | None = Field(
        default=None, description="Default embedding pipeline ID"
    )

    # Language and sampling policy
    default_language: str = Field(default="*", description="Default language code or wildcard")
    supported_languages: list[str] = Field(
        default_factory=list, description="Supported language codes"
    )
    media_sampling: MediaSamplingSettings = Field(
        default_factory=MediaSamplingSettings,
        description=(
            "Chunking/sampling policy for text, OCR, caption, and transcript "
            "representations. Part of the pipeline fingerprint (see "
            "PipelineFingerprint.sampling_fingerprint) so that changing target_tokens, "
            "overlap, or separator_hierarchy invalidates cached representations "
            "rather than silently reusing chunks produced under a different policy."
        ),
    )

    # Privacy and capability controls
    privacy_classes_enabled: list[MediaPrivacyClass] = Field(
        default_factory=list, description="Enabled privacy sensitivity classes"
    )
    require_capability_for_privacy: bool = Field(
        default=True, description="Whether capabilities are required for privacy-classified content"
    )

    # Resource limits
    global_max_concurrent_jobs: int = Field(
        default=4, ge=1, description="Global concurrent job limit"
    )
    workspace_max_concurrent_jobs: int = Field(
        default=2, ge=1, description="Per-workspace concurrent job limit"
    )

    # Feature flags
    enable_image_processing: bool = Field(
        default=False, description="Enable image processing pipelines"
    )
    enable_audio_processing: bool = Field(
        default=False, description="Enable audio processing pipelines"
    )
    enable_video_processing: bool = Field(
        default=False, description="Enable video processing pipelines"
    )
    enable_document_ocr: bool = Field(default=False, description="Enable document OCR pipeline")
    enable_visual_embeddings: bool = Field(
        default=False, description="Enable visual embedding generation"
    )
    enable_cross_modal_retrieval: bool = Field(
        default=False, description="Enable cross-modal text-to-visual retrieval"
    )


# =============================================================================
# Integration with Existing Chunk/Extraction Models
# =============================================================================


class LegacyTextRepresentation(ImmutableModel):
    """Conversion wrapper for existing Chunk/Extraction models.

    This provides compatibility with existing text-only workflows while
    exposing the new representation model.
    """

    chunk_id: str | None = Field(default=None, description="Legacy chunk ID if applicable")
    extraction_id: str | None = Field(
        default=None, description="Legacy extraction ID if applicable"
    )
    representation: DerivedRepresentation = Field(description="New representation model")
    migration_provenance: str = Field(
        default="legacy_migration", description="Marker for migrated content"
    )


def chunk_to_representation(
    chunk_id: str,
    resource_version_id: ResourceVersionId,
    text_content: str,
    created_at: datetime,
    source_content_hash: ContentHash,
) -> DerivedRepresentation:
    """Convert a legacy chunk to a DerivedRepresentation.

    This function preserves existing Chunk data while exposing the new
    representation model for multimedia integration.

    Args:
        chunk_id: Legacy chunk identifier
        resource_version_id: Source resource version
        text_content: Text content of the chunk
        created_at: When the chunk was created
        source_content_hash: Hash of the source content

    Returns:
        A DerivedRepresentation compatible with the new model
    """
    from uuid import uuid4

    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=created_at,
        updated_at=created_at,
        textual_payload=text_content,
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="legacy_chunk",
            adapter_version="v1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name="legacy_chunk",
            adapter_version="v1",
            sampling_fingerprint="legacy_chunk_v1",
        ),
    )


def extraction_to_representation(
    extraction_id: str,
    resource_version_id: ResourceVersionId,
    extracted_text: str,
    created_at: datetime,
    source_content_hash: ContentHash,
) -> DerivedRepresentation:
    """Convert a legacy Extraction to a DerivedRepresentation.

    This function preserves existing Extraction data while exposing the new
    representation model for multimedia integration.

    Args:
        extraction_id: Legacy extraction identifier
        resource_version_id: Source resource version
        extracted_text: Extracted text content
        created_at: When the extraction was created
        source_content_hash: Hash of the source content

    Returns:
        A DerivedRepresentation compatible with the new model
    """
    from uuid import uuid4

    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=created_at,
        updated_at=created_at,
        textual_payload=extracted_text,
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="legacy_extraction",
            adapter_version="v1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name="legacy_extraction",
            adapter_version="v1",
            sampling_fingerprint="legacy_extraction_v1",
        ),
    )
