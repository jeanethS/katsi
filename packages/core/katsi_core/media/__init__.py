"""Multimedia understanding integration module.

This module provides strict contracts for multimedia representations that
integrate with the agentic-workspace-coordination system. It maintains
backward compatibility with existing text-only workflows while enabling
media-aware processing.

Key exports:
- DerivedRepresentation: Core model for multimedia representations
- EvidenceLocatorUnion: Discriminated union for spatial/temporal locators
- MediaProcessingConfig: Configuration for media pipelines
- RepresentationRegistry: Authoritative registry for representations
- BlobStore: Content-addressed blob storage
- Conversion functions for legacy Chunk/Extraction compatibility
"""

from katsi_core.media.blob_store import (
    BlobMetadata,
    BlobReference,
    BlobReferenceFactory,
    BlobStore,
)
from katsi_core.media.contracts import (
    # Core models
    DerivedRepresentation,
    EmbeddingSpaceFingerprint,
    EvidenceLocatorUnion,
    ImageRegionLocator,
    MediaCoverage,
    # Descriptors and metadata
    MediaDescriptor,
    MediaMimePattern,
    MediaPipelineDefinition,
    MediaPrivacyClass,
    # Configuration
    MediaProcessingConfig,
    MediaProducerType,
    # Enums
    MediaRepresentationKind,
    MediaRepresentationStatus,
    MediaTypeFamily,
    PageLocator,
    PipelineFingerprint,
    PipelineStage,
    # Producer and pipeline
    ProducerProvenance,
    RepresentationError,
    SceneLocator,
    TextRangeLocator,
    TimeRangeLocator,
    VideoFrameLocator,
    # Evidence locators
    WholeResourceLocator,
    # Legacy compatibility
    chunk_to_representation,
    compute_sampling_fingerprint,
    extraction_to_representation,
)
from katsi_core.media.governed_operations import (
    DerivedMediaArtifactExecutor,
    DerivedMediaOperationError,
    MaterializedMediaArtifact,
)
from katsi_core.media.privacy import (
    UntrustedMediaEvidence,
    classify_metadata,
    redact_sensitive_metadata,
    render_untrusted_media_prompt,
    require_sensitive_media_access,
)
from katsi_core.media.registry import (
    RepresentationLifecycleManager,
    RepresentationRegistry,
)

__all__ = [
    # Core models
    "DerivedRepresentation",
    "EvidenceLocatorUnion",
    # Evidence locators
    "WholeResourceLocator",
    "TextRangeLocator",
    "PageLocator",
    "ImageRegionLocator",
    "TimeRangeLocator",
    "VideoFrameLocator",
    "SceneLocator",
    # Descriptors and metadata
    "MediaDescriptor",
    "MediaCoverage",
    "RepresentationError",
    # Producer and pipeline
    "ProducerProvenance",
    "PipelineFingerprint",
    # Configuration
    "MediaProcessingConfig",
    "MediaPipelineDefinition",
    "MediaMimePattern",
    # Enums
    "MediaRepresentationKind",
    "MediaRepresentationStatus",
    "MediaTypeFamily",
    "MediaPrivacyClass",
    "MediaProducerType",
    "PipelineStage",
    "EmbeddingSpaceFingerprint",
    # Registry and storage
    "RepresentationRegistry",
    "RepresentationLifecycleManager",
    "BlobStore",
    "BlobReference",
    "BlobReferenceFactory",
    "BlobMetadata",
    # Legacy compatibility
    "chunk_to_representation",
    "extraction_to_representation",
    "compute_sampling_fingerprint",
    "UntrustedMediaEvidence",
    "classify_metadata",
    "redact_sensitive_metadata",
    "render_untrusted_media_prompt",
    "require_sensitive_media_access",
    "DerivedMediaArtifactExecutor",
    "DerivedMediaOperationError",
    "MaterializedMediaArtifact",
]

__version__ = "0.1.0"
