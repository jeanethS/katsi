"""Tests for multimedia integration contracts.

This test suite validates that all multimedia contracts:
1. Have strict validation and reject invalid inputs
2. Round-trip correctly through JSON serialization
3. Integrate properly with existing workspace coordination contracts
4. Preserve legacy Chunk/Extraction compatibility
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from katsi_core.config import ChunkingThresholds, MediaSamplingSettings
from katsi_core.media.contracts import (
    DerivedRepresentation,
    EmbeddingSpaceFingerprint,
    EvidenceLocatorUnion,
    ImageRegionLocator,
    MediaCoverage,
    MediaDescriptor,
    MediaMimePattern,
    MediaPipelineDefinition,
    MediaPrivacyClass,
    MediaProcessingConfig,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    MediaTypeFamily,
    PageLocator,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    SceneLocator,
    TextRangeLocator,
    TimeRangeLocator,
    VideoFrameLocator,
    WholeResourceLocator,
    chunk_to_representation,
    compute_sampling_fingerprint,
    extraction_to_representation,
)
from katsi_core.workspace.contracts import ResourceVersionId

NOW = datetime(2026, 8, 17, tzinfo=UTC)
HASH = "a" * 64
RESOURCE_VERSION_ID: ResourceVersionId = uuid4()  # type: ignore
REPRESENTATION_ID = uuid4()


# =============================================================================
# Strict Enum Tests
# =============================================================================


def test_media_representation_kind_enum_is_complete() -> None:
    """Ensure all expected representation kinds are defined."""
    expected_kinds = {
        "metadata",
        "media_descriptor",
        "extracted_text",
        "ocr_text",
        "image_caption",
        "transcript_segment",
        "thumbnail",
        "keyframe",
        "scene",
        "silence_span",
        "visual_region",
        "visual_embedding",
        "text_embedding",
        "proxy_media",
    }

    actual_kinds = {kind.value for kind in MediaRepresentationKind}

    assert actual_kinds == expected_kinds


def test_media_representation_status_enum_is_complete() -> None:
    """Ensure all expected status values are defined."""
    expected_statuses = {"pending", "current", "partial", "unavailable", "failed"}

    actual_statuses = {status.value for status in MediaRepresentationStatus}

    assert actual_statuses == expected_statuses


def test_media_type_family_enum_is_complete() -> None:
    """Ensure all expected media families are defined."""
    expected_families = {"image", "audio", "video", "document", "text", "unknown"}

    actual_families = {family.value for family in MediaTypeFamily}

    assert actual_families == expected_families


# =============================================================================
# Evidence Locator Tests
# =============================================================================


def test_whole_resource_locator_round_trips_as_json() -> None:
    """WholeResourceLocator should serialize and deserialize correctly."""
    locator = WholeResourceLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
    )

    restored = WholeResourceLocator.model_validate_json(locator.model_dump_json())

    assert restored == locator


def test_text_range_locator_validates_range() -> None:
    """TextRangeLocator should reject invalid character ranges."""
    with pytest.raises(ValidationError, match="end_char must be greater than start_char"):
        TextRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_char=100,
            end_char=50,
        )

    with pytest.raises(ValidationError, match="end_char must be greater than start_char"):
        TextRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_char=50,
            end_char=50,
        )


def test_text_range_locator_accepts_valid_range() -> None:
    """TextRangeLocator should accept valid character ranges."""
    locator = TextRangeLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        start_char=0,
        end_char=100,
    )

    assert locator.start_char == 0
    assert locator.end_char == 100


def test_page_locator_normalizes_bounding_boxes() -> None:
    """PageLocator should validate normalized bounding boxes."""
    # Valid normalized bounding box
    locator = PageLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        page_number=1,
        bounding_box=(0.1, 0.2, 0.3, 0.4),
    )

    assert locator.bounding_box == (0.1, 0.2, 0.3, 0.4)

    # Invalid: coordinates outside [0, 1]
    with pytest.raises(ValidationError, match="Bounding box coordinates must be normalized"):
        PageLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            page_number=1,
            bounding_box=(1.5, 0.2, 0.3, 0.4),
        )


def test_image_region_locator_validates_coordinates() -> None:
    """ImageRegionLocator should validate normalized coordinates."""
    # Valid region
    locator = ImageRegionLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        bounding_box=(0.0, 0.0, 1.0, 1.0),
    )

    assert locator.bounding_box == (0.0, 0.0, 1.0, 1.0)

    # Invalid: negative coordinates
    with pytest.raises(ValidationError, match="Bounding box must be normalized"):
        ImageRegionLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            bounding_box=(-0.1, 0.0, 1.0, 1.0),
        )

    # Invalid: exceeds bounds
    with pytest.raises(ValidationError, match="Bounding box must be normalized"):
        ImageRegionLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            bounding_box=(0.5, 0.5, 0.6, 0.7),
        )


def test_time_range_locator_validates_milliseconds() -> None:
    """TimeRangeLocator should validate time ranges."""
    # Valid range
    locator = TimeRangeLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        start_ms=0,
        end_ms=5000,
    )

    assert locator.start_ms == 0
    assert locator.end_ms == 5000

    # Invalid: end before start
    with pytest.raises(ValidationError, match="end_ms must be greater than start_ms"):
        TimeRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_ms=5000,
            end_ms=3000,
        )


def test_video_frame_locator_accepts_optional_frame_index() -> None:
    """VideoFrameLocator should accept with and without frame index."""
    # With frame index
    locator_with_index = VideoFrameLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        timestamp_ms=1000,
        frame_index=42,
    )

    assert locator_with_index.frame_index == 42

    # Without frame index
    locator_without_index = VideoFrameLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        timestamp_ms=1000,
    )

    assert locator_without_index.frame_index is None


def test_scene_locator_validates_range_and_keyframes() -> None:
    """SceneLocator should validate time ranges and accept keyframe IDs."""
    keyframe_ids = (uuid4(), uuid4(), uuid4())

    locator = SceneLocator(
        resource_version_id=RESOURCE_VERSION_ID,
        representation_id=REPRESENTATION_ID,
        start_ms=0,
        end_ms=10000,
        keyframe_ids=keyframe_ids,
    )

    assert locator.start_ms == 0
    assert locator.end_ms == 10000
    assert len(locator.keyframe_ids) == 3

    # Invalid: end before start
    with pytest.raises(ValidationError, match="end_ms must be greater than start_ms"):
        SceneLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_ms=10000,
            end_ms=5000,
        )


def test_all_locators_round_trip_as_json() -> None:
    """All locator types should serialize and deserialize correctly."""
    locators = [
        WholeResourceLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
        ),
        TextRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_char=0,
            end_char=100,
        ),
        PageLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            page_number=5,
            bounding_box=(0.1, 0.2, 0.3, 0.4),
        ),
        ImageRegionLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            bounding_box=(0.0, 0.0, 0.5, 0.5),
        ),
        TimeRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_ms=0,
            end_ms=5000,
        ),
        VideoFrameLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            timestamp_ms=1000,
            frame_index=42,
        ),
        SceneLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_ms=0,
            end_ms=10000,
            keyframe_ids=(uuid4(), uuid4()),
        ),
    ]

    for locator in locators:
        restored = type(locator).model_validate_json(locator.model_dump_json())
        assert restored == locator


# =============================================================================
# Media Descriptor Tests
# =============================================================================


def test_media_descriptor_validates_dimensions() -> None:
    """MediaDescriptor should validate dimension constraints."""
    # Valid image descriptor
    descriptor = MediaDescriptor(
        mime_type="image/png",
        extension_hint="png",
        family=MediaTypeFamily.IMAGE,
        width=1920,
        height=1080,
    )

    assert descriptor.width == 1920
    assert descriptor.height == 1080

    # Invalid: zero dimensions
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        MediaDescriptor(
            mime_type="image/png",
            extension_hint="png",
            family=MediaTypeFamily.IMAGE,
            width=0,
            height=1080,
        )


def test_media_descriptor_accepts_duration_and_page_count() -> None:
    """MediaDescriptor should accept duration and page count where appropriate."""
    # Audio descriptor with duration
    audio_descriptor = MediaDescriptor(
        mime_type="audio/mpeg",
        extension_hint="mp3",
        family=MediaTypeFamily.AUDIO,
        duration_ms=180000,  # 3 minutes
        codec="mp3",
    )

    assert audio_descriptor.duration_ms == 180000

    # Document descriptor with page count
    doc_descriptor = MediaDescriptor(
        mime_type="application/pdf",
        extension_hint="pdf",
        family=MediaTypeFamily.DOCUMENT,
        page_count=42,
    )

    assert doc_descriptor.page_count == 42


def test_media_descriptor_tracks_warnings() -> None:
    """MediaDescriptor should track various warning states."""
    descriptor = MediaDescriptor(
        mime_type="application/pdf",
        extension_hint="exe",  # Wrong extension
        family=MediaTypeFamily.DOCUMENT,
        page_count=10,
        extension_mismatch=True,
        password_protected=True,
    )

    assert descriptor.extension_mismatch is True
    assert descriptor.password_protected is True


def test_media_descriptor_round_trips_as_json() -> None:
    """MediaDescriptor should serialize and deserialize correctly."""
    descriptor = MediaDescriptor(
        mime_type="image/jpeg",
        extension_hint="jpg",
        family=MediaTypeFamily.IMAGE,
        width=3840,
        height=2160,
        duration_ms=15000,
        codec="h264",
    )

    restored = MediaDescriptor.model_validate_json(descriptor.model_dump_json())

    assert restored == descriptor


# =============================================================================
# Coverage and Error Tests
# =============================================================================


def test_media_coverage_validates_completeness() -> None:
    """MediaCoverage should validate completeness relationships."""
    # Complete representation
    coverage = MediaCoverage(is_complete=True, coverage_fraction=1.0)

    assert coverage.is_complete is True
    assert coverage.coverage_fraction == 1.0

    # Inconsistent: complete but not full coverage
    with pytest.raises(ValidationError, match="is_complete=True requires coverage_fraction=1.0"):
        MediaCoverage(is_complete=True, coverage_fraction=0.8)

    # Partial coverage (valid)
    partial_coverage = MediaCoverage(
        is_complete=False, coverage_fraction=0.6, detail="First half only"
    )

    assert partial_coverage.is_complete is False
    assert partial_coverage.coverage_fraction == 0.6


def test_representation_error_round_trips_as_json() -> None:
    """RepresentationError should serialize and deserialize correctly."""
    error = RepresentationError(
        error_category="unsupported_format",
        error_message="Codec not available",
        is_retriable=False,
        diagnostic_info={"codec": "h265", "reason": "Hardware decoder missing"},
    )

    restored = RepresentationError.model_validate_json(error.model_dump_json())

    assert restored == error


# =============================================================================
# Producer and Pipeline Fingerprint Tests
# =============================================================================


def test_producer_provenance_extracts_fingerprint_components() -> None:
    """ProducerProvenance should extract components for fingerprinting."""
    producer = ProducerProvenance(
        producer_type=MediaProducerType.MODEL_BACKED,
        adapter_name="clip_encoder",
        adapter_version="v1.5",
        model_identity="clip-vit-base-32",
        model_version="2023-12-01",
    )

    components = producer.get_fingerprint_components()

    assert components["adapter_name"] == "clip_encoder"
    assert components["adapter_version"] == "v1.5"
    assert components["model_identity"] == "clip-vit-base-32"
    assert components["model_version"] == "2023-12-01"


def test_pipeline_fingerprint_extracts_cache_components() -> None:
    """PipelineFingerprint should extract components for cache keys."""
    fingerprint = PipelineFingerprint(
        source_content_hash=HASH,
        representation_kind=MediaRepresentationKind.VISUAL_EMBEDDING,
        stage=PipelineStage.EMBED_VISUAL,
        adapter_name="clip_encoder",
        adapter_version="v1.5",
        model_identity="clip-vit-base-32",
        model_version="2023-12-01",
        sampling_fingerprint="sampling_v1",
        language_policy="en",
        ocr_language="en",
        prompt_version="prompt_v1",
    )

    components = fingerprint.get_cache_key_components()

    assert components["source_hash"] == HASH
    assert components["kind"] == "visual_embedding"
    assert components["stage"] == "embed_visual"
    assert components["model"] == "clip-vit-base-32@2023-12-01"
    assert components["language"] == "en"


def test_pipeline_fingerprint_round_trips_as_json() -> None:
    """PipelineFingerprint should serialize and deserialize correctly."""
    fingerprint = PipelineFingerprint(
        source_content_hash=HASH,
        representation_kind=MediaRepresentationKind.OCR_TEXT,
        stage=PipelineStage.OCR,
        adapter_name="tesseract",
        adapter_version="5.3.0",
        model_identity=None,
        sampling_fingerprint="sampling_v1",
        language_policy="en",
        ocr_language="en",
    )

    restored = PipelineFingerprint.model_validate_json(fingerprint.model_dump_json())

    assert restored == fingerprint


# =============================================================================
# Derived Representation Tests
# =============================================================================


def test_derived_representation_requires_textual_payload_for_text_kinds() -> None:
    """DerivedRepresentation should require textual_payload for text-based kinds."""
    with pytest.raises(ValidationError, match="representations require textual_payload"):
        DerivedRepresentation(
            id=uuid4(),
            resource_version_id=RESOURCE_VERSION_ID,
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=NOW,
            updated_at=NOW,
            textual_payload=None,
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="test",
                adapter_version="v1",
            ),
            pipeline_fingerprint=PipelineFingerprint(
                source_content_hash=HASH,
                representation_kind=MediaRepresentationKind.OCR_TEXT,
                stage=PipelineStage.OCR,
                adapter_name="test",
                adapter_version="v1",
                sampling_fingerprint="v1",
            ),
        )


def test_derived_representation_requires_blob_for_visual_kinds() -> None:
    """DerivedRepresentation should require blob_reference for visual kinds."""
    with pytest.raises(
        ValidationError, match="representations require blob_reference and blob_hash"
    ):
        DerivedRepresentation(
            id=uuid4(),
            resource_version_id=RESOURCE_VERSION_ID,
            kind=MediaRepresentationKind.THUMBNAIL,
            media_type="image/png",
            status=MediaRepresentationStatus.CURRENT,
            created_at=NOW,
            updated_at=NOW,
            textual_payload="unexpected text",
            blob_reference=None,
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="test",
                adapter_version="v1",
            ),
            pipeline_fingerprint=PipelineFingerprint(
                source_content_hash=HASH,
                representation_kind=MediaRepresentationKind.THUMBNAIL,
                stage=PipelineStage.GENERATE_THUMBNAIL,
                adapter_name="test",
                adapter_version="v1",
                sampling_fingerprint="v1",
            ),
        )


def test_derived_representation_validates_error_status() -> None:
    """DerivedRepresentation should require error information for failed/unavailable status."""
    with pytest.raises(ValidationError, match="must include error information"):
        DerivedRepresentation(
            id=uuid4(),
            resource_version_id=RESOURCE_VERSION_ID,
            kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
            media_type="text/plain",
            status=MediaRepresentationStatus.FAILED,
            created_at=NOW,
            updated_at=NOW,
            textual_payload="Partial transcript...",
            error=None,  # Missing error for failed status
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
                adapter_name="whisper",
                adapter_version="v3",
            ),
            pipeline_fingerprint=PipelineFingerprint(
                source_content_hash=HASH,
                representation_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
                stage=PipelineStage.TRANSCRIBE,
                adapter_name="whisper",
                adapter_version="v3",
                sampling_fingerprint="v1",
            ),
        )


def test_derived_representation_round_trips_as_json() -> None:
    """DerivedRepresentation should serialize and deserialize correctly."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=RESOURCE_VERSION_ID,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=NOW,
        updated_at=NOW,
        textual_payload="Extracted text from document page 5",
        locators=(
            PageLocator(
                resource_version_id=RESOURCE_VERSION_ID,
                representation_id=REPRESENTATION_ID,
                page_number=5,
                bounding_box=(0.1, 0.2, 0.3, 0.4),
            ),
        ),
        coverage=MediaCoverage(
            is_complete=False, coverage_fraction=0.8, detail="Page 5 of 6 processed"
        ),
        confidence=0.95,
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="tesseract",
            adapter_version="5.3.0",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=HASH,
            representation_kind=MediaRepresentationKind.OCR_TEXT,
            stage=PipelineStage.OCR,
            adapter_name="tesseract",
            adapter_version="5.3.0",
            sampling_fingerprint="sampling_v1",
            language_policy="en",
            ocr_language="en",
        ),
    )

    restored = DerivedRepresentation.model_validate_json(representation.model_dump_json())

    assert restored == representation


def test_derived_representation_is_immutable() -> None:
    """DerivedRepresentation should be immutable."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=RESOURCE_VERSION_ID,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=NOW,
        updated_at=NOW,
        textual_payload="Sample text",
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="test",
            adapter_version="v1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=HASH,
            representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name="test",
            adapter_version="v1",
            sampling_fingerprint="v1",
        ),
    )

    with pytest.raises(ValidationError, match="frozen_instance"):
        representation.textual_payload = "Modified text"


# =============================================================================
# Configuration Model Tests
# =============================================================================


def test_media_pipeline_definition_validates_constraints() -> None:
    """MediaPipelineDefinition should validate resource and execution constraints."""
    pipeline = MediaPipelineDefinition(
        id="ocr_default",
        name="Default OCR Pipeline",
        description="Tesseract-based OCR for documents",
        stage=PipelineStage.OCR,
        accepted_mime_patterns=["application/pdf", "image/*"],
        representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
        producer_type=MediaProducerType.MODEL_BACKED,
        model_identity="tesseract-5.3.0",
        timeout_seconds=120.0,
        max_output_bytes=50_000_000,
        max_pages=100,
        network_disabled=True,
        shell_enabled=False,
    )

    assert pipeline.id == "ocr_default"
    assert pipeline.network_disabled is True
    assert pipeline.shell_enabled is False


def test_media_pipeline_definition_rejects_invalid_timeouts() -> None:
    """MediaPipelineDefinition should reject non-positive timeouts."""
    with pytest.raises(ValidationError, match="greater than 0"):
        MediaPipelineDefinition(
            id="test",
            name="Test",
            stage=PipelineStage.CAPTION,
            accepted_mime_patterns=["image/*"],
            representation_kinds_produced=[MediaRepresentationKind.IMAGE_CAPTION],
            producer_type=MediaProducerType.MODEL_BACKED,
            timeout_seconds=0,  # Invalid
        )


def test_media_processing_config_aggregates_settings() -> None:
    """MediaProcessingConfig should aggregate all media processing settings."""
    config = MediaProcessingConfig(
        enabled_mime_patterns=[
            MediaMimePattern(pattern="image/*", enabled=True),
            MediaMimePattern(pattern="audio/*", enabled=True),
        ],
        pipelines=[
            MediaPipelineDefinition(
                id="image_caption",
                name="Image Captioning",
                stage=PipelineStage.CAPTION,
                accepted_mime_patterns=["image/*"],
                representation_kinds_produced=[MediaRepresentationKind.IMAGE_CAPTION],
                producer_type=MediaProducerType.MODEL_BACKED,
                model_identity="blip-base",
                timeout_seconds=60.0,
            )
        ],
        default_ocr_pipeline="ocr_default",
        default_caption_pipeline="image_caption",
        default_language="en",
        supported_languages=["en", "es", "fr"],
        privacy_classes_enabled=[MediaPrivacyClass.LOCATION],
        require_capability_for_privacy=True,
        global_max_concurrent_jobs=4,
        workspace_max_concurrent_jobs=2,
        enable_image_processing=True,
        enable_audio_processing=True,
        enable_video_processing=False,
        enable_document_ocr=True,
    )

    assert config.default_ocr_pipeline == "ocr_default"
    assert config.enable_image_processing is True
    assert config.enable_video_processing is False
    assert len(config.pipelines) == 1


def test_media_processing_config_round_trips_as_json() -> None:
    """MediaProcessingConfig should serialize and deserialize correctly."""
    config = MediaProcessingConfig(
        enabled_mime_patterns=[
            MediaMimePattern(pattern="image/*", enabled=True),
        ],
        pipelines=[
            MediaPipelineDefinition(
                id="test",
                name="Test Pipeline",
                stage=PipelineStage.CAPTION,
                accepted_mime_patterns=["image/*"],
                representation_kinds_produced=[MediaRepresentationKind.IMAGE_CAPTION],
                producer_type=MediaProducerType.MODEL_BACKED,
                timeout_seconds=60.0,
            )
        ],
        default_language="en",
        global_max_concurrent_jobs=4,
        enable_image_processing=True,
    )

    restored = MediaProcessingConfig.model_validate_json(config.model_dump_json())

    assert restored == config


def test_media_processing_config_defaults_media_sampling() -> None:
    """MediaProcessingConfig should default to a valid MediaSamplingSettings."""
    config = MediaProcessingConfig()

    assert isinstance(config.media_sampling, MediaSamplingSettings)
    assert config.media_sampling.chunking.target_tokens == 512


def test_media_processing_config_accepts_custom_media_sampling() -> None:
    """MediaProcessingConfig should accept an overridden chunking policy."""
    config = MediaProcessingConfig(
        media_sampling=MediaSamplingSettings(
            chunking=ChunkingThresholds(target_tokens=1024, overlap=128)
        )
    )

    assert config.media_sampling.chunking.target_tokens == 1024

    restored = MediaProcessingConfig.model_validate_json(config.model_dump_json())
    assert restored == config


def test_compute_sampling_fingerprint_is_deterministic() -> None:
    """compute_sampling_fingerprint should be stable for identical settings."""
    settings_a = MediaSamplingSettings(chunking=ChunkingThresholds(target_tokens=512, overlap=64))
    settings_b = MediaSamplingSettings(chunking=ChunkingThresholds(target_tokens=512, overlap=64))

    assert compute_sampling_fingerprint(settings_a) == compute_sampling_fingerprint(settings_b)


def test_compute_sampling_fingerprint_changes_with_policy() -> None:
    """Different chunking policies must yield different fingerprints (Decision 16)."""
    default_settings = MediaSamplingSettings()
    changed_settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(target_tokens=1024, overlap=64)
    )

    assert compute_sampling_fingerprint(default_settings) != compute_sampling_fingerprint(
        changed_settings
    )


# =============================================================================
# Legacy Integration Tests
# =============================================================================


def test_chunk_to_representation_preserves_data() -> None:
    """chunk_to_representation should preserve chunk data in new model."""
    chunk_id = "legacy_chunk_123"
    text_content = "This is legacy chunk content"

    representation = chunk_to_representation(
        chunk_id=chunk_id,
        resource_version_id=RESOURCE_VERSION_ID,
        text_content=text_content,
        created_at=NOW,
        source_content_hash=HASH,
    )

    assert representation.kind == MediaRepresentationKind.EXTRACTED_TEXT
    assert representation.media_type == "text/plain"
    assert representation.status == MediaRepresentationStatus.CURRENT
    assert representation.textual_payload == text_content
    assert representation.producer.adapter_name == "legacy_chunk"
    assert representation.pipeline_fingerprint.stage == PipelineStage.EXTRACT_TEXT


def test_extraction_to_representation_preserves_data() -> None:
    """extraction_to_representation should preserve extraction data in new model."""
    extraction_id = "legacy_extraction_456"
    extracted_text = "Extracted document text"

    representation = extraction_to_representation(
        extraction_id=extraction_id,
        resource_version_id=RESOURCE_VERSION_ID,
        extracted_text=extracted_text,
        created_at=NOW,
        source_content_hash=HASH,
    )

    assert representation.kind == MediaRepresentationKind.EXTRACTED_TEXT
    assert representation.media_type == "text/plain"
    assert representation.status == MediaRepresentationStatus.CURRENT
    assert representation.textual_payload == extracted_text
    assert representation.producer.adapter_name == "legacy_extraction"
    assert representation.pipeline_fingerprint.stage == PipelineStage.EXTRACT_TEXT


def test_legacy_conversions_round_trip_as_json() -> None:
    """Legacy conversion representations should serialize correctly."""
    representation = chunk_to_representation(
        chunk_id="test_chunk",
        resource_version_id=RESOURCE_VERSION_ID,
        text_content="Test content",
        created_at=NOW,
        source_content_hash=HASH,
    )

    restored = DerivedRepresentation.model_validate_json(representation.model_dump_json())

    assert restored == representation


# =============================================================================
# Integration with Existing Configuration
# =============================================================================


def test_media_sampling_settings_provides_fingerprint_components() -> None:
    """MediaSamplingSettings should extract fingerprint components."""
    settings = MediaSamplingSettings()

    components = settings.get_fingerprint_components()

    assert "chunking_target_tokens" in components
    assert "chunking_overlap" in components
    assert "chunking_separators" in components
    assert components["chunking_target_tokens"] == 512
    assert components["chunking_overlap"] == 64


def test_media_sampling_settings_validates_chunking_policy() -> None:
    """MediaSamplingSettings should validate chunking constraints."""
    from katsi_core.config import ChunkingThresholds

    # Valid policy
    settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(
            target_tokens=256,
            overlap=32,
            separator_hierarchy=["\n\n", "\n", ". ", " ", ""],
        )
    )

    assert settings.chunking.target_tokens == 256

    # Invalid: overlap too large
    with pytest.raises(ValidationError, match="overlap should not exceed 50%"):
        MediaSamplingSettings(
            chunking=ChunkingThresholds(
                target_tokens=100,
                overlap=80,  # 80% overlap
            )
        )


# =============================================================================
# Comprehensive Invalid Input Tests
# =============================================================================


@pytest.mark.parametrize(
    "invalid_coordinates,expected_error",
    [
        # Invalid text ranges
        ((100, 50), "end_char must be greater than start_char"),
        ((-1, 100), "greater than or equal to 0"),
        # Invalid image regions
        ((-0.1, 0.0, 1.0, 1.0), "Bounding box must be normalized"),
        ((0.0, 0.0, 0.0, 1.0), "Bounding box must be normalized"),  # zero width
        ((0.0, 0.0, 1.0, 0.0), "Bounding box must be normalized"),  # zero height
        ((0.5, 0.5, 0.6, 0.7), "Bounding box must be normalized"),  # exceeds bounds
        # Invalid time ranges
        ((5000, 3000), "end_ms must be greater than start_ms"),
        ((-100, 5000), "greater than or equal to 0"),
        # Invalid page coordinates
        ((0.1, 0.2, 0.3, 1.5), "Bounding box coordinates must be normalized"),
    ],
)
def test_evidence_locators_reject_invalid_coordinates(invalid_coordinates, expected_error) -> None:
    """Evidence locators should reject invalid coordinates systematically."""
    locator_type, *coords = invalid_coordinates

    if locator_type == "text":
        with pytest.raises(ValidationError, match=expected_error):
            TextRangeLocator(
                resource_version_id=RESOURCE_VERSION_ID,
                representation_id=REPRESENTATION_ID,
                start_char=coords[0],
                end_char=coords[1],
            )
    elif locator_type == "image":
        with pytest.raises(ValidationError, match=expected_error):
            ImageRegionLocator(
                resource_version_id=RESOURCE_VERSION_ID,
                representation_id=REPRESENTATION_ID,
                bounding_box=coords,
            )
    elif locator_type == "time":
        with pytest.raises(ValidationError, match=expected_error):
            TimeRangeLocator(
                resource_version_id=RESOURCE_VERSION_ID,
                representation_id=REPRESENTATION_ID,
                start_ms=coords[0],
                end_ms=coords[1],
            )
    elif locator_type == "page":
        with pytest.raises(ValidationError, match=expected_error):
            PageLocator(
                resource_version_id=RESOURCE_VERSION_ID,
                representation_id=REPRESENTATION_ID,
                page_number=1,
                bounding_box=coords,
            )


def test_invalid_media_coverage_rejected() -> None:
    """Invalid coverage specifications should be rejected."""
    # Inconsistent completeness
    with pytest.raises(ValidationError, match="is_complete=True requires coverage_fraction=1.0"):
        MediaCoverage(is_complete=True, coverage_fraction=0.7)

    # Invalid coverage fraction - Pydantic's built-in ge/le validators take precedence
    with pytest.raises(ValidationError):
        MediaCoverage(is_complete=False, coverage_fraction=-0.1)

    with pytest.raises(ValidationError):
        MediaCoverage(is_complete=False, coverage_fraction=1.5)


def test_invalid_pipeline_fingerprint_rejected() -> None:
    """Invalid pipeline fingerprints should be rejected."""
    # Missing required fields would be caught by Pydantic
    with pytest.raises(ValidationError):
        PipelineFingerprint(
            source_content_hash="invalid",  # Invalid hash format
            representation_kind=MediaRepresentationKind.OCR_TEXT,
            stage=PipelineStage.OCR,
            adapter_name="test",
            adapter_version="v1",
            sampling_fingerprint="v1",
        )


# =============================================================================
# Integration with Workspace Contracts
# =============================================================================


def test_representation_integration_with_resource_version() -> None:
    """DerivedRepresentation should integrate with ResourceVersion contract."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=RESOURCE_VERSION_ID,
        kind=MediaRepresentationKind.METADATA,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=NOW,
        updated_at=NOW,
        textual_payload='{"width": 1920, "height": 1080}',
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="metadata_extractor",
            adapter_version="v1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=HASH,
            representation_kind=MediaRepresentationKind.METADATA,
            stage=PipelineStage.EXTRACT_METADATA,
            adapter_name="metadata_extractor",
            adapter_version="v1",
            sampling_fingerprint="v1",
        ),
    )

    # Should be able to reference this from ClaimEvidence
    assert representation.resource_version_id == RESOURCE_VERSION_ID
    assert representation.id is not None


# =============================================================================
# Enum and Type Safety Tests
# =============================================================================


def test_all_enums_have_distinct_values() -> None:
    """All enum values should be distinct to avoid collisions."""
    from katsi_core.media.contracts import (
        MediaPrivacyClass,
        MediaProducerType,
        MediaRepresentationKind,
        MediaRepresentationStatus,
        MediaTypeFamily,
        PipelineStage,
    )

    # Collect all enum values across all enums
    all_values = []
    for enum_class in [
        MediaRepresentationKind,
        MediaRepresentationStatus,
        MediaTypeFamily,
        MediaPrivacyClass,
        MediaProducerType,
        PipelineStage,
        EmbeddingSpaceFingerprint,
    ]:
        all_values.extend([member.value for member in enum_class])

    # Filter out expected duplicates across different enum types
    # (these are OK since they're in different enum classes)
    allowed_cross_enum_duplicates = {
        "unknown",  # MediaTypeFamily.UNKNOWN and EmbeddingSpaceFingerprint.UNKNOWN_SPACE
        "caption",  # PipelineStage.CAPTION and (was) MediaRepresentationKind.CAPTION
    }

    # Check for unexpected duplicates within the same enum type
    value_counts = {}
    for value in all_values:
        value_counts[value] = value_counts.get(value, 0) + 1

    # Only flag duplicates that aren't expected cross-enum duplicates
    unexpected_duplicates = [
        value
        for value, count in value_counts.items()
        if count > 1 and value not in allowed_cross_enum_duplicates
    ]

    assert not unexpected_duplicates, (
        f"Unexpected duplicate enum values found: {unexpected_duplicates}"
    )

    # Check for duplicates
    duplicates = [value for value in set(all_values) if all_values.count(value) > 1]

    assert not duplicates, f"Duplicate enum values found: {duplicates}"


def test_discriminated_union_resolves_all_locator_types() -> None:
    """EvidenceLocatorUnion discriminator should resolve all locator types."""
    # This test ensures the discriminated union works correctly
    locators = [
        WholeResourceLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
        ),
        TextRangeLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            start_char=0,
            end_char=100,
        ),
        PageLocator(
            resource_version_id=RESOURCE_VERSION_ID,
            representation_id=REPRESENTATION_ID,
            page_number=1,
        ),
    ]

    for locator in locators:
        # Should be able to serialize and deserialize through union
        json_data = locator.model_dump_json()
        # Parse back through the union type
        from pydantic import TypeAdapter

        adapter = TypeAdapter(EvidenceLocatorUnion)
        restored = adapter.validate_json(json_data)
        assert type(restored) is type(locator)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
