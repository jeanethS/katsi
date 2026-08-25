"""Execute configured root media pipelines for current workspace resources."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from katsi_core.media.adapter_catalog import build_media_pipeline_registry
from katsi_core.media.cache import RepresentationCache
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaPipelineDefinition,
    MediaProcessingConfig,
    MediaRepresentationKind,
)
from katsi_core.media.execution import PipelineExecutionOrchestrator
from katsi_core.media.fingerprint import _stable_digest, build_pipeline_fingerprint
from katsi_core.media.pipeline_registry import RegisteredPipeline
from katsi_core.media.protocols import MediaPipelineProtocol
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.media.video_pipeline import ScenePlan, build_scene_representations


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
        # Availability probes spawn a subprocess; adapters are stateless. Both
        # are per-pipeline facts, not per-file ones, so resolve each once.
        self._adapters: dict[str, MediaPipelineProtocol | None] = {}

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
        candidates.sort(key=lambda pipeline: pipeline.definition.stage.value != "extract_metadata")
        if not candidates:
            counts.unavailable += 1
            return counts
        produced: dict[MediaRepresentationKind, list[DerivedRepresentation]] = {}
        duration_ms: int | None = None
        for pipeline in candidates:
            adapter = self._adapter_for(pipeline)
            if adapter is None:
                counts.unavailable += 1
                continue
            definition = pipeline.definition
            for kind in definition.representation_kinds_produced:
                # Scene batches expand to many cited SceneLocators, so one cached
                # representation is insufficient to represent the generation.
                use_cache = kind is not MediaRepresentationKind.SCENE
                fingerprint = build_pipeline_fingerprint(
                    source_content_hash=content_hash,
                    representation_kind=kind,
                    stage=definition.stage,
                    adapter_name=adapter.get_adapter_name(),
                    adapter_version=adapter.get_adapter_version(),
                    model_identity=definition.model_identity,
                    settings=self._config.media_sampling,
                    executable_policy=_executable_policy_digest(definition),
                )
                cached = (
                    self._cache.get_or_mark_miss(resource_version_id, kind, fingerprint)
                    if use_cache
                    else None
                )
                if cached is not None:
                    if cached.kind is MediaRepresentationKind.MEDIA_DESCRIPTOR:
                        duration_ms = _duration_ms(cached)
                    counts.reused += 1
                    continue
                result = self._orchestrator.run(
                    adapter,
                    definition,
                    file_path,
                    resource_version_id,
                    content_hash,
                    fingerprint,
                )
                if result.kind is MediaRepresentationKind.MEDIA_DESCRIPTOR:
                    duration_ms = _duration_ms(result)
                if (
                    result.kind is MediaRepresentationKind.SCENE
                    and result.status.value == "current"
                ):
                    if duration_ms is None:
                        counts.unavailable += 1
                        continue
                    produced.setdefault(result.kind, []).extend(
                        _expand_scenes(
                            result,
                            duration_ms,
                            resource_version_id,
                            content_hash,
                            self._config,
                        )
                    )
                else:
                    produced.setdefault(result.kind, []).append(result)
                if result.status.value in {"failed", "unavailable"}:
                    counts.failed += 1
                else:
                    counts.processed += 1
        for representations in produced.values():
            self._representations.register_representation_batch(representations)
        return counts

    def _adapter_for(self, pipeline: RegisteredPipeline) -> MediaPipelineProtocol | None:
        """Resolve a pipeline's adapter once, or ``None`` when it is unavailable."""
        pipeline_id = pipeline.definition.id
        if pipeline_id not in self._adapters:
            available, _ = pipeline.is_available()
            self._adapters[pipeline_id] = pipeline.build_adapter() if available else None
        return self._adapters[pipeline_id]


def _executable_policy_digest(definition: MediaPipelineDefinition) -> str:
    """Digest the owner-configured executable policy.

    The executable path and argument template are inputs to the pipeline's
    output exactly as much as the model or sampling policy: changing the tool,
    its arguments (e.g. the OCR language), or its limits must invalidate
    cached output rather than silently reusing text produced under another
    configuration.
    """
    return _stable_digest(
        {
            "executable_path": definition.executable_path,
            "fixed_args": list(definition.fixed_args),
            "availability_probe": definition.availability_probe,
        }
    )


def _duration_ms(representation: DerivedRepresentation) -> int | None:
    try:
        value = json.loads(representation.textual_payload or "{}").get("duration_ms")
        return int(value) if isinstance(value, int | float) and value > 0 else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _expand_scenes(
    batch: DerivedRepresentation,
    duration_ms: int,
    resource_version_id: UUID,
    content_hash: str,
    config: MediaProcessingConfig,
) -> tuple[DerivedRepresentation, ...]:
    try:
        boundaries = json.loads(batch.textual_payload or "{}").get("boundaries_ms", [])
        points = sorted(
            {0, duration_ms, *(int(point) for point in boundaries if 0 < int(point) < duration_ms)}
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    scenes = tuple(
        ScenePlan(start, end, (start + end) // 2)
        for start, end in zip(points, points[1:], strict=False)
    )
    return build_scene_representations(
        scenes,
        {},
        (),
        resource_version_id=resource_version_id,
        source_content_hash=content_hash,
        settings=config.media_sampling,
        adapter_name=batch.producer.adapter_name,
        adapter_version=batch.producer.adapter_version,
    )
