from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.config import SQLiteSettings
from katsi_core.media.contracts import (
    MediaDescriptor,
    MediaMimePattern,
    MediaPipelineDefinition,
    MediaProcessingConfig,
    MediaProducerType,
    MediaRepresentationKind,
    MediaTypeFamily,
    PipelineStage,
)
from katsi_core.media.migration import (
    LegacyTextRepresentationMigrator,
    MediaFeatureGate,
    MediaMetadataReconciler,
    MediaReconciliationInput,
)
from katsi_core.media.pipeline_registry import MediaPipelineRegistry
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.store import LegacyFileRecordImporter, WorkspaceRepository, apply_migrations
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


class _AvailableAdapter:
    @classmethod
    def check_availability(cls) -> tuple[bool, str | None]:
        return True, None


class _Detector:
    def __init__(self, descriptor: MediaDescriptor) -> None:
        self._descriptor = descriptor

    def detect_media(self, file_path: Path, content_hash: str) -> MediaDescriptor:
        return self._descriptor


def _registry(tmp_path: Path) -> RepresentationRegistry:
    return RepresentationRegistry(WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings()))


def _pipeline() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="image-metadata",
        name="Image metadata",
        stage=PipelineStage.EXTRACT_METADATA,
        accepted_mime_patterns=["image/*"],
        representation_kinds_produced=[MediaRepresentationKind.METADATA],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="metadata-reader",
    )


def test_legacy_text_migration_is_idempotent_and_leaves_legacy_retrieval_untouched(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    migrator = LegacyTextRepresentationMigrator(registry)
    resource_version_id = uuid4()

    first = migrator.import_text(
        legacy_id="file:0",
        resource_version_id=resource_version_id,
        content_hash="a" * 64,
        text="legacy chunk text",
    )

    assert first is not None
    assert first.producer.adapter_name == "legacy_text_migration"
    assert (
        migrator.import_text(
            legacy_id="file:0",
            resource_version_id=resource_version_id,
            content_hash="a" * 64,
            text="legacy chunk text",
        )
        is None
    )
    assert registry.get_current_representation(
        resource_version_id, MediaRepresentationKind.EXTRACTED_TEXT
    )


def test_legacy_file_import_populates_private_representation_state(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "note.md"
    source.write_text("legacy", encoding="utf-8")
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Workspace")
    legacy_path = tmp_path / "file_records.json"
    legacy_path.write_text(
        json.dumps(
            {
                "legacy-note": {
                    "id": "legacy-note",
                    "path": str(source),
                    "name": "note.md",
                    "ext": ".md",
                    "mime": "text/markdown",
                    "size_bytes": 6,
                    "mtime": source.stat().st_mtime,
                    "content_hash": "a" * 64,
                    "summary": "migrated summary",
                }
            }
        ),
        encoding="utf-8",
    )
    registry = RepresentationRegistry(database)

    assert (
        LegacyFileRecordImporter(database, repository, registry).import_file(
            legacy_path, workspace.id, "legacy-v1"
        )
        == 1
    )
    resource = repository.list_current_resources(workspace.id)[0]
    with database.connection() as connection:
        version_id = connection.execute(
            "SELECT id FROM resource_versions WHERE resource_id = ?", (str(resource.id),)
        ).fetchone()[0]
    assert (
        registry.get_current_representation(
            UUID(version_id), MediaRepresentationKind.EXTRACTED_TEXT
        )
        is not None
    )
    # A legacy-only binary has no registry dependency and must leave private
    # representation state untouched when it sees already-migrated data.
    assert (
        LegacyFileRecordImporter(database, repository).import_file(
            legacy_path, workspace.id, "legacy-v1"
        )
        == 0
    )
    assert (
        registry.get_current_representation(
            UUID(version_id), MediaRepresentationKind.EXTRACTED_TEXT
        )
        is not None
    )


def test_feature_gate_only_enables_patterns_with_available_required_pipeline() -> None:
    registry = MediaPipelineRegistry()
    registry.register(_pipeline(), _AvailableAdapter)
    config = MediaProcessingConfig(
        enabled_mime_patterns=[
            MediaMimePattern(pattern="image/*", required_pipeline="image-metadata"),
            MediaMimePattern(pattern="audio/*", required_pipeline="missing"),
        ]
    )
    gate = MediaFeatureGate(config, registry)

    assert gate.accepts("image/png")
    assert not gate.accepts("audio/wav")
    rollback = gate.disable_media()
    assert not rollback.enable_image_processing
    assert not rollback.enable_visual_embeddings


def test_reconciliation_records_descriptor_or_unavailable_without_semantic_stage(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource_version_id = uuid4()
    reconciler = MediaMetadataReconciler(
        registry,
        _Detector(
            MediaDescriptor(
                mime_type="application/pdf", family=MediaTypeFamily.DOCUMENT, encrypted=True
            )
        ),
    )

    representation = reconciler.reconcile(
        MediaReconciliationInput(resource_version_id, tmp_path / "encrypted.pdf", "b" * 64)
    )

    assert representation.kind is MediaRepresentationKind.MEDIA_DESCRIPTOR
    assert representation.status.value == "unavailable"
    assert representation.error is not None
