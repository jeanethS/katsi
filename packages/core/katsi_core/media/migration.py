"""Compatibility helpers for introducing media representations safely.

The helpers in this module deliberately do not alter legacy chunk/vector
projections.  They populate the private representation registry alongside the
old path, and feature gates make media opt-in only after dependencies are
available.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaDescriptor,
    MediaMimePattern,
    MediaProcessingConfig,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    WholeResourceLocator,
)
from katsi_core.media.pipeline_registry import MediaPipelineRegistry
from katsi_core.media.registry import RepresentationRegistry


class MediaDetector(Protocol):
    """The small detector surface needed during metadata-only reconciliation."""

    def detect_media(self, file_path: Path, content_hash: str) -> MediaDescriptor: ...


@dataclass(frozen=True)
class MediaReconciliationInput:
    """One already-tracked resource to reconcile without re-ingesting it."""

    resource_version_id: UUID
    path: Path
    content_hash: str


class MediaFeatureGate:
    """Resolves opt-in media support without loading optional dependencies."""

    def __init__(self, config: MediaProcessingConfig, registry: MediaPipelineRegistry) -> None:
        self._config = config
        self._registry = registry

    def enabled_patterns(self) -> tuple[MediaMimePattern, ...]:
        """Return enabled MIME patterns whose required pipeline is available."""
        available = set(self._registry.available_pipeline_ids())
        return tuple(
            pattern
            for pattern in self._config.enabled_mime_patterns
            if pattern.enabled
            and (pattern.required_pipeline is None or pattern.required_pipeline in available)
        )

    def accepts(self, mime_type: str) -> bool:
        """Whether a detected media type may enter media processing."""
        return any(fnmatch.fnmatch(mime_type, item.pattern) for item in self.enabled_patterns())

    def disable_media(self) -> MediaProcessingConfig:
        """Return rollback configuration without touching manifests or blobs."""
        return self._config.model_copy(
            update={
                "enable_image_processing": False,
                "enable_audio_processing": False,
                "enable_video_processing": False,
                "enable_document_ocr": False,
                "enable_visual_embeddings": False,
                "enable_cross_modal_retrieval": False,
            }
        )


class LegacyTextRepresentationMigrator:
    """Imports legacy extracted text as a private representation exactly once."""

    _ADAPTER_NAME = "legacy_text_migration"
    _ADAPTER_VERSION = "v1"

    def __init__(self, registry: RepresentationRegistry) -> None:
        self._registry = registry

    def import_text(
        self,
        *,
        legacy_id: str,
        resource_version_id: UUID,
        content_hash: str,
        text: str,
        created_at: datetime | None = None,
    ) -> DerivedRepresentation | None:
        """Persist text only if this legacy item has not already been imported."""
        if not text:
            return None
        fingerprint = PipelineFingerprint(
            source_content_hash=content_hash,
            representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name=self._ADAPTER_NAME,
            adapter_version=self._ADAPTER_VERSION,
            sampling_fingerprint=f"legacy:{legacy_id}",
        )
        existing = self._registry.find_cached_representation(
            resource_version_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
        )
        if existing is not None:
            return None
        now = created_at or datetime.now(UTC)
        representation_id = uuid4()
        representation = DerivedRepresentation(
            id=representation_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.EXTRACTED_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=text,
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=representation_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name=self._ADAPTER_NAME,
                adapter_version=self._ADAPTER_VERSION,
            ),
            pipeline_fingerprint=fingerprint,
        )
        self._registry.register_representation(representation)
        return representation


class MediaMetadataReconciler:
    """Creates descriptor or unavailable state before semantic media stages run."""

    def __init__(self, registry: RepresentationRegistry, detector: MediaDetector) -> None:
        self._registry = registry
        self._detector = detector

    def reconcile(self, item: MediaReconciliationInput) -> DerivedRepresentation:
        """Record safe deterministic metadata; never queue OCR/caption/transcription."""
        descriptor = self._detector.detect_media(item.path, item.content_hash)
        now = datetime.now(UTC)
        representation_id = uuid4()
        unavailable = (
            descriptor.encrypted
            or descriptor.password_protected
            or descriptor.malformed
            or descriptor.family.value == "unknown"
        )
        status = (
            MediaRepresentationStatus.UNAVAILABLE
            if unavailable
            else MediaRepresentationStatus.CURRENT
        )
        error = None
        if unavailable:
            reason = "encrypted" if descriptor.encrypted else "unsupported or malformed"
            error = RepresentationError(error_category="media_unavailable", error_message=reason)
        representation = DerivedRepresentation(
            id=representation_id,
            resource_version_id=item.resource_version_id,
            kind=MediaRepresentationKind.MEDIA_DESCRIPTOR,
            media_type="application/json",
            status=status,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(descriptor.model_dump(mode="json"), sort_keys=True),
            locators=(
                WholeResourceLocator(
                    resource_version_id=item.resource_version_id,
                    representation_id=representation_id,
                ),
            ),
            coverage=MediaCoverage(
                is_complete=not unavailable, coverage_fraction=1.0 if not unavailable else 0.0
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="media_metadata_reconciliation",
                adapter_version="v1",
            ),
            pipeline_fingerprint=PipelineFingerprint(
                source_content_hash=item.content_hash,
                representation_kind=MediaRepresentationKind.MEDIA_DESCRIPTOR,
                stage=PipelineStage.DETECT,
                adapter_name="media_metadata_reconciliation",
                adapter_version="v1",
                sampling_fingerprint="metadata-only-v1",
            ),
            error=error,
        )
        self._registry.register_representation(representation)
        return representation
