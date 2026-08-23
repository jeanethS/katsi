from datetime import UTC, datetime
from uuid import uuid4

from katsi_core.config import SQLiteSettings
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProcessingConfig,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
)
from katsi_core.media.execution import PipelineExecutionOrchestrator
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.media.reprocess import MediaReprocessor
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


def _definition(identifier: str) -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id=identifier,
        adapter_binding="video_scene_detect_ffmpeg",
        name="Scene detector",
        stage=PipelineStage.DETECT_SCENES,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.SCENE],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/usr/bin/true",
    )


def _metadata_definition() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="metadata",
        adapter_binding="video_metadata_ffprobe",
        name="Video metadata",
        stage=PipelineStage.EXTRACT_METADATA,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.MEDIA_DESCRIPTOR],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/usr/bin/true",
    )


def _representation(resource_version_id, fingerprint) -> DerivedRepresentation:
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.SCENE,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="{}",
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.5),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake-scene",
            adapter_version="1",
        ),
        pipeline_fingerprint=fingerprint,
    )


def test_reprocessor_batches_siblings_and_reuses_cache(tmp_path, monkeypatch) -> None:
    registry = RepresentationRegistry(
        WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    )
    config = MediaProcessingConfig(
        enable_video_processing=True,
        pipelines=[_metadata_definition(), _definition("scene-a"), _definition("scene-b")],
    )
    calls = 0

    def run(_self, _adapter, _definition, _path, resource_version_id, _hash, fingerprint):
        nonlocal calls
        calls += 1
        representation = _representation(resource_version_id, fingerprint)
        if fingerprint.representation_kind is MediaRepresentationKind.MEDIA_DESCRIPTOR:
            return representation.model_copy(
                update={
                    "kind": MediaRepresentationKind.MEDIA_DESCRIPTOR,
                    "textual_payload": '{"duration_ms": 2000}',
                }
            )
        return representation.model_copy(update={"textual_payload": '{"boundaries_ms": [1000]}'})

    monkeypatch.setattr(PipelineExecutionOrchestrator, "run", run)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    resource_version_id = uuid4()
    reprocessor = MediaReprocessor(registry, config)

    first = reprocessor.process(source, resource_version_id, "a" * 64)
    second = reprocessor.process(source, resource_version_id, "a" * 64)

    assert first.processed == 3
    assert second.reused == 1
    assert calls == 5
    current = [
        representation
        for representation in registry.get_representations_by_resource(resource_version_id)
        if registry.is_current(representation.id)
    ]
    assert len(current) == 5
    assert all(
        representation.locators
        for representation in current
        if representation.kind is MediaRepresentationKind.SCENE
    )


def test_reprocessor_reports_unavailable_without_pipelines(tmp_path) -> None:
    registry = RepresentationRegistry(
        WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    )
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    outcome = MediaReprocessor(registry, MediaProcessingConfig()).process(source, uuid4(), "a" * 64)

    assert outcome.unavailable == 1


def test_reprocessor_records_pipeline_failure(tmp_path, monkeypatch) -> None:
    registry = RepresentationRegistry(
        WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    )
    config = MediaProcessingConfig(enable_video_processing=True, pipelines=[_definition("scene")])

    def run(_self, _adapter, _definition, _path, resource_version_id, _hash, fingerprint):
        failed = _representation(resource_version_id, fingerprint)
        return failed.model_copy(
            update={
                "status": MediaRepresentationStatus.FAILED,
                "error": RepresentationError(
                    error_category="processing_error", error_message="boom", is_retriable=False
                ),
            }
        )

    monkeypatch.setattr(PipelineExecutionOrchestrator, "run", run)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")

    outcome = MediaReprocessor(registry, config).process(source, uuid4(), "a" * 64)

    assert outcome.failed == 1
