"""Governed derived-media Change Set operation contracts (tasks 12.1-12.6)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from katsi_core.config import ObserverSettings, SQLiteSettings
from katsi_core.media.contracts import (
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    PipelineStage,
)
from katsi_core.media.governed_operations import (
    DerivedMediaArtifactExecutor,
    DerivedMediaOperationError,
    MaterializedMediaArtifact,
)
from katsi_core.media.pipeline_registry import MediaPipelineRegistry
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.action_journal import ActionJournalService
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.contracts import (
    ChangeSet,
    ExportKeyframesOperation,
    ExportRepresentationOperation,
    ExportTranscriptOrOcrOperation,
    GenerateProxyMediaOperation,
    GenerateThumbnailOperation,
    ReplaceDerivedMediaArtifactOperation,
    RiskClass,
)
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.staging import AdjacentStagingManager


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _definition() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="owner.thumbnail.v1",
        name="Owner thumbnail pipeline",
        stage=PipelineStage.GENERATE_THUMBNAIL,
        accepted_mime_patterns=["image/*"],
        representation_kinds_produced=[
            MediaRepresentationKind.THUMBNAIL,
            MediaRepresentationKind.KEYFRAME,
            MediaRepresentationKind.PROXY_MEDIA,
            MediaRepresentationKind.OCR_TEXT,
        ],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/owner/configured/tool",
    )


def _operation(cls: type[GenerateThumbnailOperation], content: bytes, **extra: object):
    return cls(
        path="derived/output.bin",
        byte_count=len(content),
        source_resource_id=uuid4(),
        source_resource_version_id=uuid4(),
        source_content_hash="a" * 64,
        pipeline_id="owner.thumbnail.v1",
        pipeline_fingerprint="f" * 64,
        expected_output_media_type="image/png",
        expected_output_hash=_hash(content),
        max_output_bytes=4096,
        source_relationship="derived_from",
        **extra,
    )


def _executor(content: bytes) -> DerivedMediaArtifactExecutor:
    registry = MediaPipelineRegistry()
    registry.register(_definition())

    def materialize(operation: GenerateThumbnailOperation) -> MaterializedMediaArtifact:
        return MaterializedMediaArtifact(
            content=content,
            media_type="image/png",
            pipeline_fingerprint=operation.pipeline_fingerprint,
            source_relationship=operation.source_relationship,
        )

    return DerivedMediaArtifactExecutor(
        registry=registry,
        staging=AdjacentStagingManager(ObserverSettings()),
        materialize=materialize,
    )


def test_closed_media_operation_variants_require_immutable_pipeline_source_and_limits() -> None:
    content = b"derived thumbnail"
    common = dict(content=content)
    assert _operation(GenerateThumbnailOperation, **common).kind == "generate_thumbnail"
    assert (
        _operation(
            ExportTranscriptOrOcrOperation,
            **common,
            representation_kind="ocr_text",
        ).kind
        == "export_transcript_or_ocr"
    )
    assert (
        _operation(ExportKeyframesOperation, **common, keyframe_ids=(uuid4(),)).kind
        == "export_keyframes"
    )
    assert _operation(GenerateProxyMediaOperation, **common).kind == "generate_proxy_media"
    assert (
        _operation(ExportRepresentationOperation, **common, representation_id=uuid4()).kind
        == "export_representation"
    )
    assert (
        _operation(
            ReplaceDerivedMediaArtifactOperation,
            **common,
            expected_current_hash="b" * 64,
            derived_artifact_source_resource_id=uuid4(),
        ).kind
        == "replace_derived_media_artifact"
    )


def test_media_export_is_staged_verified_journal_ready_and_preserves_original(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.png"
    original.write_bytes(b"immutable original")
    original_hash = _hash(original.read_bytes())
    content = b"derived thumbnail"
    operation = _operation(GenerateThumbnailOperation, content)
    executor = _executor(content)
    target = tmp_path / operation.path

    stage = executor.stage(operation, target)
    assert stage["output_hash"] == _hash(content)
    assert stage["source_resource_version_id"] == str(operation.source_resource_version_id)
    assert executor.commit(operation, target)["verification"] == "sha256_and_media_type_validated"
    assert target.read_bytes() == content
    assert _hash(original.read_bytes()) == original_hash


def test_replacement_requires_exact_derived_hash_and_is_idempotently_restaged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "derived/output.bin"
    target.parent.mkdir()
    target.write_bytes(b"old derived")
    content = b"new derived"
    operation = _operation(
        ReplaceDerivedMediaArtifactOperation,
        content,
        expected_current_hash=_hash(b"old derived"),
        derived_artifact_source_resource_id=uuid4(),
    )
    executor = _executor(content)
    executor.stage(operation, target)
    executor.commit(operation, target)
    assert target.read_bytes() == content

    executor.stage(operation, target)
    with pytest.raises(DerivedMediaOperationError, match="exact-hash"):
        executor.commit(operation, target)
    assert target.read_bytes() == content


def test_invalid_output_or_materializer_failure_never_mutates_target(tmp_path: Path) -> None:
    content = b"derived"
    target = tmp_path / "derived/output.bin"
    target.parent.mkdir()
    target.write_bytes(b"existing")
    operation = _operation(GenerateThumbnailOperation, content)
    executor = _executor(content)

    with pytest.raises(DerivedMediaOperationError, match="output hash"):
        executor.stage(operation.model_copy(update={"expected_output_hash": "b" * 64}), target)
    assert target.read_bytes() == b"existing"

    def fail(_: GenerateThumbnailOperation) -> MaterializedMediaArtifact:
        raise RuntimeError("injected materializer failure")

    failing = DerivedMediaArtifactExecutor(
        registry=executor.registry,
        staging=executor.staging,
        materialize=fail,
    )
    with pytest.raises(RuntimeError, match="injected"):
        failing.stage(operation, target)
    assert target.read_bytes() == b"existing"


def test_operations_are_strict_and_cannot_contain_processing_commands() -> None:
    content = b"derived"
    operation = _operation(GenerateThumbnailOperation, content)
    with pytest.raises(ValidationError, match="Extra inputs"):
        GenerateThumbnailOperation(**operation.model_dump(), command="rm -rf /")


def test_action_journal_serializes_media_source_pipeline_and_rollback_evidence(
    tmp_path: Path,
) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    operation = _operation(GenerateThumbnailOperation, b"derived")
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Export thumbnail",
        idempotency_key="export-thumbnail-v1",
        dependencies=(),
        operations=(operation,),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    ChangeSetService(database).submit(change_set)
    outcome = ActionJournalService(database).create_planning_entry(
        change_set_id=change_set.id,
        operations=(operation,),
        affected_hashes={},
        preimages={},
    )
    assert outcome.receipt["action_journal_id"] == str(outcome.id)
