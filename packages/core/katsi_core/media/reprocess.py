"""Execute configured root media pipelines for current workspace resources."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from katsi_core.media.adapter_catalog import build_media_pipeline_registry
from katsi_core.media.cache import RepresentationCache
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaProcessingConfig,
    MediaRepresentationKind,
)
from katsi_core.media.execution import PipelineExecutionOrchestrator
from katsi_core.media.fingerprint import build_pipeline_fingerprint
from katsi_core.media.registry import RepresentationRegistry


@dataclass
class ReprocessCounts:
    processed: int = 0
    reused: int = 0
    unavailable: int = 0
    failed: int = 0
    skipped: int = 0


class MediaReprocessor:
    """Run configured root pipelines; each pipeline failure is isolated."""

    def __init__(self, registry: RepresentationRegistry, config: MediaProcessingConfig) -> None:
        self._representations = registry
        self._pipelines = build_media_pipeline_registry(config)
        self._cache = RepresentationCache(registry)
        self._config = config
        self._orchestrator = PipelineExecutionOrchestrator()

    def process(
        self, file_path: Path, resource_version_id: UUID, content_hash: str
    ) -> ReprocessCounts:
        counts = ReprocessCounts()
        mime_type, _ = mimetypes.guess_type(file_path.name)
        if mime_type is None:
            counts.skipped += 1
            return counts
        candidates = [
            pipeline
            for pipeline in self._pipelines.find_for_mime_type(mime_type)
            if not pipeline.definition.input_kinds
        ]
        if not candidates:
            counts.unavailable += 1
            return counts
        produced: dict[MediaRepresentationKind, list[DerivedRepresentation]] = {}
        for pipeline in candidates:
            available, _ = pipeline.is_available()
            if not available:
                counts.unavailable += 1
                continue
            definition = pipeline.definition
            for kind in definition.representation_kinds_produced:
                fingerprint = build_pipeline_fingerprint(
                    source_content_hash=content_hash,
                    representation_kind=kind,
                    stage=definition.stage,
                    adapter_name=definition.adapter_binding or definition.id,
                    adapter_version="1",
                    model_identity=definition.model_identity,
                    settings=self._config.media_sampling,
                )
                cached = self._cache.get_or_mark_miss(resource_version_id, kind, fingerprint)
                if cached is not None:
                    counts.reused += 1
                    continue
                result = self._orchestrator.run(
                    pipeline.build_adapter(),
                    definition,
                    file_path,
                    resource_version_id,
                    content_hash,
                    fingerprint,
                )
                produced.setdefault(result.kind, []).append(result)
                if result.status.value in {"failed", "unavailable"}:
                    counts.failed += 1
                else:
                    counts.processed += 1
        for representations in produced.values():
            self._representations.register_representation_batch(representations)
        return counts
