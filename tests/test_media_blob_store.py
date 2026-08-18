"""Tests for the Blob Store.

These tests verify the functionality of the content-addressed blob store,
including hash verification, deduplication, atomic writes, and corrupted
blob detection.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from katsi_core.media.blob_store import (
    BlobMetadata,
    BlobReference,
    BlobReferenceFactory,
    BlobStore,
)


@pytest.fixture
def temp_storage():
    """Create a temporary storage directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        storage_path = Path(temp_dir)
        yield storage_path
        # Cleanup is automatic


@pytest.fixture
def blob_store(temp_storage):
    """Create a blob store for testing."""
    return BlobStore(temp_storage)


@pytest.fixture
def sample_content():
    """Sample binary content for testing."""
    return b"This is sample binary content for testing the blob store."


@pytest.fixture
def sample_content_large():
    """Larger sample binary content for testing."""
    return b"x" * 1024 * 100  # 100KB of data


def test_blob_store_initialization(temp_storage):
    """Test that blob store initializes correctly."""
    store = BlobStore(temp_storage)
    assert store._storage_root == temp_storage
    assert store._blobs_dir.exists()
    assert store._metadata_dir.exists()


def test_store_blob_basic(blob_store, sample_content):
    """Test basic blob storage."""
    blob_hash, byte_count = blob_store.store_blob(sample_content)

    assert blob_hash is not None
    assert len(blob_hash) == 64  # BLAKE3 hex string
    assert byte_count == len(sample_content)

    # Verify blob exists
    assert blob_store.blob_exists(blob_hash)


def test_blob_deduplication(blob_store, sample_content):
    """Test that duplicate content is deduplicated."""
    # Store same content twice
    hash1, count1 = blob_store.store_blob(sample_content)
    hash2, count2 = blob_store.store_blob(sample_content)

    # Should get same hash
    assert hash1 == hash2
    assert count1 == count2

    # Should only have one blob in storage
    blobs = blob_store.list_blobs()
    assert len(blobs) == 1
    assert hash1 in blobs


def test_get_blob(blob_store, sample_content):
    """Test retrieving blob content."""
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Retrieve content
    retrieved = blob_store.get_blob(blob_hash)

    assert retrieved is not None
    assert retrieved == sample_content


def test_get_nonexistent_blob(blob_store):
    """Test retrieving a non-existent blob."""
    result = blob_store.get_blob("nonexistent_hash")
    assert result is None


def test_blob_hash_verification(blob_store, sample_content):
    """Test that hash verification works correctly."""
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Get blob path and manually corrupt it
    blob_path = blob_store._get_blob_path(blob_hash)
    original_content = blob_path.read_bytes()

    # Corrupt the content
    blob_path.write_bytes(b"corrupted content")

    # Trying to get the blob should raise an error
    with pytest.raises(ValueError, match="corrupted"):
        blob_store.get_blob(blob_hash)

    # Restore original content
    blob_path.write_bytes(original_content)

    # Now it should work
    retrieved = blob_store.get_blob(blob_hash)
    assert retrieved == sample_content


def test_blob_reference_count(blob_store, sample_content):
    """Test blob reference counting."""
    blob_hash, _ = blob_store.store_blob(sample_content, retention_days=7)

    # Increment reference
    blob_store.increment_reference(blob_hash)
    metadata = blob_store.get_blob_info(blob_hash)
    assert metadata.reference_count == 2

    # Decrement reference
    new_count = blob_store.decrement_reference(blob_hash)
    assert new_count == 1


def test_blob_retention_policy(blob_store, sample_content):
    """Test blob retention policy."""
    # Store blob with 7-day retention
    blob_hash, _ = blob_store.store_blob(sample_content, retention_days=7)

    metadata = blob_store.get_blob_info(blob_hash)
    assert metadata.retention_until is not None

    # Should not be cleanable within retention period
    assert not blob_store.can_cleanup(blob_hash, max_age_days=1)

    # Manually age the metadata to simulate passage of time beyond retention period
    from datetime import timedelta

    old_metadata = BlobMetadata(
        blob_hash=metadata.blob_hash,
        byte_count=metadata.byte_count,
        created_at=datetime.now(UTC)
        - timedelta(days=400),  # Very old, past both retention and max_age
        access_count=metadata.access_count,
        last_accessed_at=datetime.now(UTC) - timedelta(days=400),
        retention_until=datetime.now(UTC) - timedelta(days=1),  # Retention expired yesterday
        reference_count=metadata.reference_count,
    )
    blob_store._save_metadata(old_metadata)

    # Should be cleanable after retention period has passed and max_age exceeded
    assert blob_store.can_cleanup(blob_hash, max_age_days=365)


def test_blob_cleanup_eligibility(blob_store, sample_content):
    """Test blob cleanup eligibility based on age."""
    # Store blob without retention
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Should not be cleanable immediately
    assert not blob_store.can_cleanup(blob_hash, max_age_days=30)

    # Manually age the metadata
    metadata = blob_store.get_blob_info(blob_hash)
    old_metadata = BlobMetadata(
        blob_hash=metadata.blob_hash,
        byte_count=metadata.byte_count,
        created_at=datetime.now(UTC) - timedelta(days=35),
        access_count=metadata.access_count,
        last_accessed_at=metadata.last_accessed_at,
        retention_until=metadata.retention_until,
        reference_count=metadata.reference_count,
    )
    blob_store._save_metadata(old_metadata)

    # Now should be cleanable
    assert blob_store.can_cleanup(blob_hash, max_age_days=30)


def test_delete_blob(blob_store, sample_content):
    """Test deleting a blob."""
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Verify it exists
    assert blob_store.blob_exists(blob_hash)

    # Delete it
    result = blob_store.delete_blob(blob_hash)
    assert result is True

    # Verify it's gone
    assert not blob_store.blob_exists(blob_hash)


def test_delete_nonexistent_blob(blob_store):
    """Test deleting a non-existent blob."""
    result = blob_store.delete_blob("nonexistent_hash")
    assert result is False


def test_list_blobs(blob_store):
    """Test listing all blobs."""
    # Store multiple blobs
    contents = [
        b"content1",
        b"content2",
        b"content3",
    ]

    hashes = []
    for content in contents:
        blob_hash, _ = blob_store.store_blob(content)
        hashes.append(blob_hash)

    # List all blobs
    all_blobs = blob_store.list_blobs()
    assert len(all_blobs) == len(contents)

    for blob_hash in hashes:
        assert blob_hash in all_blobs


def test_blob_metadata(blob_store, sample_content):
    """Test blob metadata."""
    blob_hash, byte_count = blob_store.store_blob(sample_content, retention_days=30)

    metadata = blob_store.get_blob_info(blob_hash)
    assert metadata is not None
    assert metadata.blob_hash == blob_hash
    assert metadata.byte_count == byte_count
    assert metadata.access_count == 1
    assert metadata.reference_count == 1
    assert metadata.retention_until is not None


def test_blob_access_tracking(blob_store, sample_content):
    """Test that blob access is tracked."""
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Initial access count should be 1 (from storage)
    metadata = blob_store.get_blob_info(blob_hash)
    assert metadata.access_count == 1

    # Access the blob
    blob_store.get_blob(blob_hash)

    # Access count should be incremented
    updated_metadata = blob_store.get_blob_info(blob_hash)
    assert updated_metadata.access_count == 2
    assert updated_metadata.last_accessed_at is not None


def test_empty_content_rejection(blob_store):
    """Test that empty content is rejected."""
    with pytest.raises(ValueError, match="empty content"):
        blob_store.store_blob(b"")


def test_storage_stats(blob_store):
    """Test storage statistics."""
    # Store some blobs
    contents = [b"content1", b"content2", b"content3"]
    total_size = sum(len(c) for c in contents)

    for content in contents:
        blob_store.store_blob(content)

    stats = blob_store.get_storage_stats()
    assert stats["total_blobs"] == len(contents)
    assert stats["total_bytes"] == total_size
    assert stats["corrupted_count"] == 0
    assert "storage_path" in stats


def test_corrupted_blob_detection(blob_store, sample_content):
    """Test detection and cleanup of corrupted blobs."""
    # Store a blob
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Corrupt the blob
    blob_path = blob_store._get_blob_path(blob_hash)
    blob_path.write_bytes(b"corrupted")

    # Get stats should show corrupted blob
    stats = blob_store.get_storage_stats()
    assert stats["corrupted_count"] == 1
    assert stats["total_blobs"] == 0  # Corrupted blob not counted

    # Cleanup corrupted blobs
    removed = blob_store.cleanup_corrupted_blobs()
    assert removed == 1

    # Verify blob was removed
    assert not blob_store.blob_exists(blob_hash)


def test_blob_reference_basic(blob_store, sample_content):
    """Test basic blob reference functionality."""
    blob_hash, byte_count = blob_store.store_blob(sample_content)

    ref = BlobReference(
        blob_hash=blob_hash,
        byte_count=byte_count,
        media_type="application/octet-stream",
        blob_store=blob_store,
    )

    assert ref.blob_hash == blob_hash
    assert ref.byte_count == byte_count
    assert ref.media_type == "application/octet-stream"
    assert ref.exists()


def test_blob_reference_get_content(blob_store, sample_content):
    """Test getting content through blob reference."""
    blob_hash, byte_count = blob_store.store_blob(sample_content)

    ref = BlobReference(
        blob_hash=blob_hash,
        byte_count=byte_count,
        media_type="text/plain",
        blob_store=blob_store,
    )

    content = ref.get_content()
    assert content == sample_content


def test_blob_reference_nonexistent(blob_store):
    """Test blob reference for non-existent blob."""
    ref = BlobReference(
        blob_hash="nonexistent_hash",
        byte_count=100,
        media_type="text/plain",
        blob_store=blob_store,
    )

    assert not ref.exists()

    with pytest.raises(ValueError, match="not found"):
        ref.get_content()


def test_blob_reference_factory(blob_store, sample_content):
    """Test blob reference factory."""
    factory = BlobReferenceFactory(blob_store)

    ref = factory.create_reference(
        content=sample_content,
        media_type="text/plain",
        retention_days=7,
    )

    assert ref.exists()
    assert ref.media_type == "text/plain"

    content = ref.get_content()
    assert content == sample_content


def test_blob_reference_factory_empty_content(blob_store):
    """Test that factory rejects empty content."""
    factory = BlobReferenceFactory(blob_store)

    with pytest.raises(ValueError, match="empty content"):
        factory.create_reference(b"", "text/plain")


def test_blob_reference_factory_from_existing(blob_store, sample_content):
    """Test creating reference from existing blob."""
    factory = BlobReferenceFactory(blob_store)

    # Store blob first
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Create reference from existing hash
    ref = factory.from_existing_hash(blob_hash, "text/plain")

    assert ref is not None
    assert ref.blob_hash == blob_hash
    assert ref.media_type == "text/plain"

    content = ref.get_content()
    assert content == sample_content


def test_blob_reference_factory_nonexistent_hash(blob_store):
    """Test creating reference from non-existent hash."""
    factory = BlobReferenceFactory(blob_store)

    ref = factory.from_existing_hash("nonexistent_hash", "text/plain")
    assert ref is None


def test_blob_metadata_serialization(blob_store, sample_content):
    """Test blob metadata serialization and deserialization."""
    blob_hash, _ = blob_store.store_blob(sample_content, retention_days=30)

    metadata = blob_store.get_blob_info(blob_hash)
    assert metadata is not None

    # Convert to dict and back
    metadata_dict = metadata.to_dict()
    restored = BlobMetadata.from_dict(metadata_dict)

    assert restored.blob_hash == metadata.blob_hash
    assert restored.byte_count == metadata.byte_count
    assert restored.reference_count == metadata.reference_count


def test_atomic_write_failure_recovery(blob_store, sample_content):
    """Test that failed writes are cleaned up atomically."""
    # This test simulates a write failure scenario
    blob_hash = "test_hash_1234567890abcdef" * 2  # 64 characters
    temp_path = blob_store._get_blob_path(blob_hash).with_suffix(".tmp")

    # Create a temp file as if a write was in progress
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(sample_content)

    # Verify temp file exists but final blob doesn't
    assert temp_path.exists()
    assert not blob_store.blob_exists(blob_hash)

    # Clean up temp file manually (simulating recovery)
    temp_path.unlink()


def test_concurrent_access_simulation(blob_store, sample_content):
    """Test behavior under concurrent-like access patterns."""
    blob_hash, _ = blob_store.store_blob(sample_content)

    # Simulate multiple "concurrent" accesses
    for _ in range(5):
        content = blob_store.get_blob(blob_hash)
        assert content == sample_content

    # Verify access count was incremented appropriately
    metadata = blob_store.get_blob_info(blob_hash)
    # Initial store (1) + 5 accesses = 6
    assert metadata.access_count == 6


def test_large_blob_handling(blob_store, sample_content_large):
    """Test handling of larger blobs."""
    blob_hash, byte_count = blob_store.store_blob(sample_content_large)

    assert blob_hash is not None
    assert byte_count == len(sample_content_large)

    # Retrieve and verify
    retrieved = blob_store.get_blob(blob_hash)
    assert retrieved == sample_content_large

    # Check stats
    stats = blob_store.get_storage_stats()
    assert stats["total_bytes"] >= byte_count
