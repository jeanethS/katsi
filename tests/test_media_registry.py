"""Tests for the Representation Registry.

These tests verify the functionality of the authoritative Derived
Representation registry, including lifecycle transitions, immutability,
and source resource deletion handling.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.registry import (
    RepresentationLifecycleManager,
    RepresentationRegistry,
)
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    settings = SQLiteSettings()
    db = WorkspaceSQLite(db_path, settings)
    yield db
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def registry(temp_db):
    """Create a representation registry for testing."""
    return RepresentationRegistry(temp_db)


@pytest.fixture
def sample_resource_id():
    """Create a sample resource version ID."""
    return ResourceVersionId(str(uuid4()))


@pytest.fixture
def sample_producer():
    """Create a sample producer provenance."""
    return ProducerProvenance(
        producer_type=MediaProducerType.DETERMINISTIC,
        adapter_name="test_adapter",
        adapter_version="1.0.0",
    )


@pytest.fixture
def sample_fingerprint(sample_resource_id):
    """Create a sample pipeline fingerprint."""
    return PipelineFingerprint(
        source_content_hash=ContentHash("a" * 64),  # 64 character hex string
        representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
        stage=PipelineStage.EXTRACT_TEXT,
        adapter_name="test_adapter",
        adapter_version="1.0.0",
        sampling_fingerprint="test_sampling",
    )


def test_registry_initialization(registry):
    """Test that registry initializes correctly."""
    assert registry is not None
    # Check that schema was created
    with registry._database.connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "representations" in table_names


def test_register_pending_representation(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test registering a pending representation."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.PENDING,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Sample OCR text",  # Required for text representations
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(representation, make_current=False)

    # Verify it was stored
    retrieved = registry.get_representation(representation.id)
    assert retrieved is not None
    assert retrieved.id == representation.id
    assert retrieved.status == MediaRepresentationStatus.PENDING
    assert retrieved.kind == MediaRepresentationKind.OCR_TEXT


def test_register_current_representation(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test registering a current representation."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Sample text content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(representation, make_current=True)

    # Verify it was stored and marked as current
    retrieved = registry.get_representation(representation.id)
    assert retrieved is not None
    assert retrieved.status == MediaRepresentationStatus.CURRENT
    assert retrieved.textual_payload == "Sample text content"


def test_get_current_representation(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test retrieving the current representation for a resource and kind."""
    # Create multiple representations
    rep1 = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC) - timedelta(hours=2),
        updated_at=datetime.now(UTC) - timedelta(hours=2),
        textual_payload="Old content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    rep2 = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC) - timedelta(hours=1),
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        textual_payload="Newer content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(rep1, make_current=True)
    registry.register_representation(rep2, make_current=True)  # Should mark rep1 as non-current

    # Should get the most recent current representation
    current = registry.get_current_representation(
        sample_resource_id, MediaRepresentationKind.EXTRACTED_TEXT
    )

    assert current is not None
    assert current.textual_payload == "Newer content"


def test_get_representations_by_resource(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test retrieving all representations for a resource."""
    # Create representations with different statuses
    statuses = [
        MediaRepresentationStatus.PENDING,
        MediaRepresentationStatus.CURRENT,
        MediaRepresentationStatus.PARTIAL,
        MediaRepresentationStatus.FAILED,
    ]

    for status in statuses:
        rep = DerivedRepresentation(
            id=uuid4(),
            resource_version_id=sample_resource_id,
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            textual_payload="Sample OCR text",  # Required for text representations
            locators=(
                WholeResourceLocator(
                    resource_version_id=sample_resource_id,
                    representation_id=uuid4(),
                ),
            ),
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
            producer=sample_producer,
            pipeline_fingerprint=sample_fingerprint,
            error=RepresentationError(
                error_category="test_error", error_message="Test error message"
            )
            if status == MediaRepresentationStatus.FAILED
            else None,
        )
        registry.register_representation(rep, make_current=False)

    # Get all representations
    all_reps = registry.get_representations_by_resource(sample_resource_id)
    assert len(all_reps) == len(statuses)

    # Filter by status
    current_reps = registry.get_representations_by_resource(
        sample_resource_id, MediaRepresentationStatus.CURRENT
    )
    assert len(current_reps) == 1
    assert current_reps[0].status == MediaRepresentationStatus.CURRENT


def test_find_cached_representation(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test finding cached representations by pipeline fingerprint."""
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Cached content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(representation, make_current=True)

    # Find by pipeline fingerprint
    cached = registry.find_cached_representation(
        sample_resource_id, MediaRepresentationKind.OCR_TEXT, sample_fingerprint
    )

    assert cached is not None
    assert cached.textual_payload == "Cached content"
    assert cached.pipeline_fingerprint.adapter_name == sample_fingerprint.adapter_name


def test_find_cached_representation_with_input_representation_id(
    registry, sample_resource_id, sample_producer
):
    """Regression test: fingerprint lookups must not crash when
    ``input_representation_id`` (a UUID field) is set.

    ``register_representation`` serializes the fingerprint with
    ``model_dump(mode="json")`` so UUIDs become strings before ``json.dumps``.
    The lookup methods must serialize with the same mode, or a raw UUID
    object reaches ``json.dumps`` and raises ``TypeError``.
    """
    fingerprint_with_input = PipelineFingerprint(
        source_content_hash=ContentHash("b" * 64),
        input_representation_id=uuid4(),
        representation_kind=MediaRepresentationKind.OCR_TEXT,
        stage=PipelineStage.OCR,
        adapter_name="test_adapter",
        adapter_version="1.0.0",
        sampling_fingerprint="test_sampling",
    )

    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Cached content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=fingerprint_with_input,
    )

    registry.register_representation(representation, make_current=True)

    # Must not raise TypeError from json.dumps on a raw UUID object.
    cached = registry.find_cached_representation(
        sample_resource_id,
        MediaRepresentationKind.OCR_TEXT,
        fingerprint_with_input,
    )
    assert cached is not None
    assert cached.textual_payload == "Cached content"

    by_pipeline = registry.get_representations_by_pipeline(fingerprint_with_input)
    assert len(by_pipeline) == 1
    assert by_pipeline[0].id == representation.id


def test_update_representation_status(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test updating representation status."""
    # Create a pending representation
    representation = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.PENDING,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Sample OCR text",  # Required for text representations
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(representation, make_current=False)

    # Update to current
    error = RepresentationError(
        error_category="test_category", error_message="Test error", is_retriable=True
    )

    registry.update_representation_status(
        representation.id, MediaRepresentationStatus.FAILED, error
    )

    # Verify update
    updated = registry.get_representation(representation.id)
    assert updated is not None
    assert updated.status == MediaRepresentationStatus.FAILED
    assert updated.error is not None
    assert updated.error.error_category == "test_category"
    assert updated.error.is_retriable is True


def test_handle_resource_deletion_preserve_historical(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test handling resource deletion while preserving historical data."""
    # Create multiple representations
    representations = []
    for i in range(3):
        rep = DerivedRepresentation(
            id=uuid4(),
            resource_version_id=sample_resource_id,
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=datetime.now(UTC) - timedelta(hours=i),
            updated_at=datetime.now(UTC) - timedelta(hours=i),
            textual_payload=f"Content {i}",
            locators=(
                WholeResourceLocator(
                    resource_version_id=sample_resource_id,
                    representation_id=uuid4(),
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=sample_producer,
            pipeline_fingerprint=sample_fingerprint,
        )
        representations.append(rep)
        registry.register_representation(rep, make_current=True)

    # Handle deletion with preservation
    registry.handle_resource_deletion(sample_resource_id, preserve_historical=True)

    # Historical data should still exist
    all_reps = registry.get_representations_by_resource(sample_resource_id)
    assert len(all_reps) == 3

    # But no current representations should exist
    current = registry.get_current_representation(
        sample_resource_id, MediaRepresentationKind.OCR_TEXT
    )
    assert current is None


def test_handle_resource_deletion_remove_all(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test handling resource deletion with complete removal."""
    # Create representations
    rep = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="Content",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    registry.register_representation(rep, make_current=True)

    # Handle deletion without preservation
    registry.handle_resource_deletion(sample_resource_id, preserve_historical=False)

    # All data should be removed
    all_reps = registry.get_representations_by_resource(sample_resource_id)
    assert len(all_reps) == 0


def test_lifecycle_manager_transitions(
    temp_db, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test lifecycle manager state transitions."""
    registry = RepresentationRegistry(temp_db)
    manager = RepresentationLifecycleManager(registry)

    # Create pending representation
    pending = manager.create_pending_representation(
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    assert pending.status == MediaRepresentationStatus.PENDING

    # Transition to current
    current = manager.transition_to_current(
        pending.id,
        textual_payload="Completed content",
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        confidence=0.95,
    )

    assert current.status == MediaRepresentationStatus.CURRENT
    assert current.textual_payload == "Completed content"
    assert current.confidence == 0.95


def test_lifecycle_manager_partial_transition(
    temp_db, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test lifecycle manager transition to partial status."""
    registry = RepresentationRegistry(temp_db)
    manager = RepresentationLifecycleManager(registry)

    # Create pending representation
    pending = manager.create_pending_representation(
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        media_type="text/plain",
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    # Transition to partial
    partial = manager.transition_to_partial(
        pending.id,
        coverage=MediaCoverage(
            is_complete=False, coverage_fraction=0.7, detail="Processed 70% of audio"
        ),
        textual_payload="Partial transcript",
    )

    assert partial.status == MediaRepresentationStatus.PARTIAL
    assert partial.coverage.coverage_fraction == 0.7
    assert partial.textual_payload == "Partial transcript"


def test_lifecycle_manager_failed_transition(
    temp_db, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test lifecycle manager transition to failed status."""
    registry = RepresentationRegistry(temp_db)
    manager = RepresentationLifecycleManager(registry)

    # Create pending representation
    pending = manager.create_pending_representation(
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    # Transition to failed
    error = RepresentationError(
        error_category="processing_error", error_message="OCR processing failed", is_retriable=False
    )

    failed = manager.transition_to_failed(pending.id, error)

    assert failed.status == MediaRepresentationStatus.FAILED
    assert failed.error is not None
    assert failed.error.error_category == "processing_error"
    assert failed.error.is_retriable is False


def test_lifecycle_manager_unavailable_transition(
    temp_db, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test lifecycle manager transition to unavailable status."""
    registry = RepresentationRegistry(temp_db)
    manager = RepresentationLifecycleManager(registry)

    # Create pending representation
    pending = manager.create_pending_representation(
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    # Transition to unavailable
    error = RepresentationError(
        error_category="unsupported_format",
        error_message="File format is not supported",
        is_retriable=False,
    )

    unavailable = manager.transition_to_unavailable(pending.id, error)

    assert unavailable.status == MediaRepresentationStatus.UNAVAILABLE
    assert unavailable.error is not None
    assert unavailable.error.error_category == "unsupported_format"


def test_invalid_lifecycle_transitions(
    temp_db, sample_resource_id, sample_producer, sample_fingerprint
):
    """Test that invalid lifecycle transitions raise errors."""
    registry = RepresentationRegistry(temp_db)
    manager = RepresentationLifecycleManager(registry)

    # Try to transition a non-existent representation
    with pytest.raises(ValueError, match="not found"):
        manager.transition_to_current(uuid4(), textual_payload="test")

    # Create a current representation and try invalid transition
    current = manager.create_pending_representation(
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )

    current = manager.transition_to_current(current.id, textual_payload="test")

    # Try to transition from current to current (invalid)
    with pytest.raises(ValueError, match="is not pending"):
        manager.transition_to_current(current.id, textual_payload="test2")


def test_immutable_replacement_preserves_history(
    registry, sample_resource_id, sample_producer, sample_fingerprint
):
    """Registering a new current representation must not mutate the old row.

    The registry replaces which representation is "current" for a
    resource/kind pair, but the superseded representation must remain
    queryable with its original immutable content intact.
    """
    first = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="first version text",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )
    registry.register_representation(first, make_current=True)

    second = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=sample_resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="second version text",
        locators=(
            WholeResourceLocator(
                resource_version_id=sample_resource_id,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )
    registry.register_representation(second, make_current=True)

    # Only the second representation is current.
    current = registry.get_current_representation(
        sample_resource_id, MediaRepresentationKind.OCR_TEXT
    )
    assert current is not None
    assert current.id == second.id
    assert current.textual_payload == "second version text"

    # The first representation is still retrievable by id, unmodified.
    preserved = registry.get_representation(first.id)
    assert preserved is not None
    assert preserved.textual_payload == "first version text"
    assert preserved.status == MediaRepresentationStatus.CURRENT

    # Both rows still exist in full resource history.
    all_reps = registry.get_representations_by_resource(sample_resource_id)
    assert {r.id for r in all_reps} == {first.id, second.id}


def test_source_version_provenance_cache_reuse(registry, sample_producer, sample_fingerprint):
    """Representations tied to different resource versions but sharing a
    pipeline fingerprint (same source content hash, adapter, and policy)
    remain individually addressable by fingerprint, supporting cache reuse
    across A -> B -> A resource-version histories and copied media without
    losing which resource version actually produced each representation.
    """
    version_a = ResourceVersionId(str(uuid4()))
    version_b = ResourceVersionId(str(uuid4()))

    rep_a = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=version_a,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="shared content",
        locators=(
            WholeResourceLocator(
                resource_version_id=version_a,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )
    registry.register_representation(rep_a, make_current=True)

    rep_b = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=version_b,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="shared content",
        locators=(
            WholeResourceLocator(
                resource_version_id=version_b,
                representation_id=uuid4(),
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=sample_producer,
        pipeline_fingerprint=sample_fingerprint,
    )
    registry.register_representation(rep_b, make_current=True)

    # Global fingerprint lookup surfaces both, preserving each one's
    # originating resource-version provenance.
    by_fingerprint = registry.get_representations_by_pipeline(sample_fingerprint)
    resource_ids = {r.resource_version_id for r in by_fingerprint}
    assert resource_ids == {version_a, version_b}

    # Each resource version's cache lookup is scoped to its own provenance.
    cached_a = registry.find_cached_representation(
        version_a, MediaRepresentationKind.EXTRACTED_TEXT, sample_fingerprint
    )
    cached_b = registry.find_cached_representation(
        version_b, MediaRepresentationKind.EXTRACTED_TEXT, sample_fingerprint
    )
    assert cached_a is not None and cached_a.id == rep_a.id
    assert cached_b is not None and cached_b.id == rep_b.id
    assert cached_a.resource_version_id == version_a
    assert cached_b.resource_version_id == version_b
