"""Private content-addressed derived-blob store.

This module provides secure storage for derived binary content with hash
verification, deduplication, atomic writes, and configured retention.

Key features:
- Content-addressed storage using BLAKE3 hashes
- Automatic deduplication by hash
- Atomic writes with verification
- Configured retention metadata
- Support for corrupted blob detection
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from uuid import UUID

from katsi_core.media.contracts import ContentHash


def _compute_hash(content: bytes) -> str:
    """Compute BLAKE3 hash of content.

    Args:
        content: Bytes to hash

    Returns:
        Hexadecimal hash string
    """
    return hashlib.blake3b(content).hexdigest()


def _verify_hash(content: bytes, expected_hash: str) -> bool:
    """Verify content matches expected hash.

    Args:
        content: Content to verify
        expected_hash: Expected hash value

    Returns:
        True if hash matches, False otherwise
    """
    return _compute_hash(content) == expected_hash


class BlobMetadata:
    """Metadata for a stored blob."""

    def __init__(
        self,
        blob_hash: str,
        byte_count: int,
        created_at: datetime,
        access_count: int,
        last_accessed_at: datetime | None = None,
        retention_until: datetime | None = None,
        reference_count: int = 1,
    ) -> None:
        """Initialize blob metadata.

        Args:
            blob_hash: Content hash
            byte_count: Size in bytes
            created_at: Creation timestamp
            access_count: Number of times accessed
            last_accessed_at: Last access timestamp
            retention_until: Retention deadline
            reference_count: Number of representations referencing this blob
        """
        self.blob_hash = blob_hash
        self.byte_count = byte_count
        self.created_at = created_at
        self.access_count = access_count
        self.last_accessed_at = last_accessed_at
        self.retention_until = retention_until
        self.reference_count = reference_count

    def to_dict(self) -> dict:
        """Convert to dictionary for storage."""
        return {
            "blob_hash": self.blob_hash,
            "byte_count": self.byte_count,
            "created_at": self.created_at.isoformat(),
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at.isoformat() if self.last_accessed_at else None,
            "retention_until": self.retention_until.isoformat() if self.retention_until else None,
            "reference_count": self.reference_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BlobMetadata":
        """Create from dictionary."""
        return cls(
            blob_hash=data["blob_hash"],
            byte_count=data["byte_count"],
            created_at=datetime.fromisoformat(data["created_at"]),
            access_count=data["access_count"],
            last_accessed_at=datetime.fromisoformat(data["last_accessed_at"]) if data.get("last_accessed_at") else None,
            retention_until=datetime.fromisoformat(data["retention_until"]) if data.get("retention_until") else None,
            reference_count=data.get("reference_count", 1),
        )


class BlobStore:
    """Private content-addressed derived-blob store.

    This store provides secure, deduplicated storage for binary content
    produced by media processing pipelines. Each blob is stored by its
    content hash, ensuring automatic deduplication and integrity verification.

    Features:
    - Content-addressed storage using BLAKE3 hashes
    - Automatic deduplication by hash
    - Atomic writes with hash verification
    - Configured retention metadata
    - Corrupted blob detection
    """

    def __init__(self, storage_root: Path) -> None:
        """Initialize the blob store.

        Args:
            storage_root: Root directory for blob storage
        """
        self._storage_root = storage_root
        self._blobs_dir = storage_root / "blobs"
        self._metadata_dir = storage_root / "metadata"

        # Create directories
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_dir.mkdir(parents=True, exist_ok=True)

    def _get_blob_path(self, blob_hash: str) -> Path:
        """Get storage path for a blob hash.

        Args:
            blob_hash: Content hash

        Returns:
            Path where blob is stored
        """
        # Use first 2 chars as directory for better filesystem performance
        prefix = blob_hash[:2]
        return self._blobs_dir / prefix / blob_hash

    def _get_metadata_path(self, blob_hash: str) -> Path:
        """Get metadata path for a blob hash.

        Args:
            blob_hash: Content hash

        Returns:
            Path where metadata is stored
        """
        prefix = blob_hash[:2]
        return self._metadata_dir / f"{prefix}_{blob_hash}.json"

    def store_blob(
        self,
        content: bytes,
        retention_days: int | None = None,
    ) -> tuple[str, int]:
        """Store content as a blob with deduplication.

        Args:
            content: Binary content to store
            retention_days: Optional retention period in days

        Returns:
            Tuple of (blob_hash, byte_count)

        Raises:
            ValueError: If content is empty or hash verification fails
        """
        if not content:
            raise ValueError("Cannot store empty content")

        blob_hash = _compute_hash(content)
        byte_count = len(content)
        timestamp = datetime.now(UTC)

        # Check if blob already exists (deduplication)
        blob_path = self._get_blob_path(blob_hash)
        if blob_path.exists():
            # Update metadata for existing blob
            self._update_metadata(blob_hash, access_increment=1)
            return blob_hash, byte_count

        # Create parent directory if needed
        blob_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file first (atomic write)
        temp_path = blob_path.with_suffix(".tmp")
        try:
            temp_path.write_bytes(content)

            # Verify hash before committing
            if not _verify_hash(temp_path.read_bytes(), blob_hash):
                temp_path.unlink()
                raise ValueError("Hash verification failed during write")

            # Atomic rename
            temp_path.replace(blob_path)

        except Exception:
            # Clean up temp file on error
            if temp_path.exists():
                temp_path.unlink()
            raise

        # Create metadata
        retention_until = None
        if retention_days is not None:
            from datetime import timedelta
            retention_until = timestamp + timedelta(days=retention_days)

        metadata = BlobMetadata(
            blob_hash=blob_hash,
            byte_count=byte_count,
            created_at=timestamp,
            access_count=1,
            last_accessed_at=timestamp,
            retention_until=retention_until,
        )

        self._save_metadata(metadata)

        return blob_hash, byte_count

    def get_blob(self, blob_hash: str) -> bytes | None:
        """Retrieve blob content by hash.

        Args:
            blob_hash: Content hash

        Returns:
            Blob content if found, None otherwise

        Raises:
            ValueError: If blob is corrupted (hash mismatch)
        """
        blob_path = self._get_blob_path(blob_hash)

        if not blob_path.exists():
            return None

        content = blob_path.read_bytes()

        # Verify content integrity
        if not _verify_hash(content, blob_hash):
            # Mark as corrupted
            self._mark_corrupted(blob_hash)
            raise ValueError(f"Blob {blob_hash} is corrupted (hash mismatch)")

        # Update access metadata
        self._update_metadata(blob_hash, access_increment=1)

        return content

    def blob_exists(self, blob_hash: str) -> bool:
        """Check if blob exists.

        Args:
            blob_hash: Content hash

        Returns:
            True if blob exists, False otherwise
        """
        return self._get_blob_path(blob_hash).exists()

    def get_blob_info(self, blob_hash: str) -> BlobMetadata | None:
        """Get metadata for a blob.

        Args:
            blob_hash: Content hash

        Returns:
            Blob metadata if found, None otherwise
        """
        metadata_path = self._get_metadata_path(blob_hash)

        if not metadata_path.exists():
            return None

        try:
            data = json.loads(metadata_path.read_text())
            return BlobMetadata.from_dict(data)
        except Exception:
            return None

    def has_blob(self, blob_hash: str) -> bool:
        """Check if blob exists and is not corrupted.

        Args:
            blob_hash: Content hash

        Returns:
            True if blob exists and is valid
        """
        blob_path = self._get_blob_path(blob_hash)
        if not blob_path.exists():
            return False

        # Verify content integrity
        try:
            content = blob_path.read_bytes()
            return _verify_hash(content, blob_hash)
        except Exception:
            return False

    def delete_blob(self, blob_hash: str) -> bool:
        """Delete a blob from storage.

        Args:
            blob_hash: Content hash

        Returns:
            True if blob was deleted, False if it didn't exist
        """
        blob_path = self._get_blob_path(blob_hash)
        metadata_path = self._get_metadata_path(blob_hash)

        deleted = False
        if blob_path.exists():
            blob_path.unlink()
            deleted = True

        if metadata_path.exists():
            metadata_path.unlink()

        return deleted

    def list_blobs(self) -> set[str]:
        """List all blob hashes in storage.

        Returns:
            Set of blob hashes
        """
        blobs = set()
        for prefix_dir in self._blobs_dir.iterdir():
            if prefix_dir.is_dir():
                for blob_file in prefix_dir.iterdir():
                    if blob_file.is_file():
                        blobs.add(blob_file.name)
        return blobs

    def can_cleanup(self, blob_hash: str, max_age_days: int) -> bool:
        """Check if a blob can be cleaned up based on age and retention.

        Args:
            blob_hash: Content hash
            max_age_days: Maximum age in days for cleanup

        Returns:
            True if blob can be cleaned up
        """
        metadata = self.get_blob_info(blob_hash)
        if metadata is None:
            return True

        # Check retention deadline
        if metadata.retention_until is not None:
            if datetime.now(UTC) < metadata.retention_until:
                return False

        # Check age
        from datetime import timedelta
        max_age = timedelta(days=max_age_days)
        age = datetime.now(UTC) - metadata.created_at

        return age > max_age

    def increment_reference(self, blob_hash: str) -> None:
        """Increment reference count for a blob.

        Args:
            blob_hash: Content hash
        """
        metadata = self.get_blob_info(blob_hash)
        if metadata is not None:
            metadata.reference_count += 1
            self._save_metadata(metadata)

    def decrement_reference(self, blob_hash: str) -> int:
        """Decrement reference count for a blob.

        Args:
            blob_hash: Content hash

        Returns:
            New reference count (0 if blob can be deleted)
        """
        metadata = self.get_blob_info(blob_hash)
        if metadata is None:
            return 0

        metadata.reference_count = max(0, metadata.reference_count - 1)
        self._save_metadata(metadata)

        return metadata.reference_count

    def get_storage_stats(self) -> dict:
        """Get storage statistics.

        Returns:
            Dictionary with storage statistics
        """
        total_blobs = 0
        total_bytes = 0
        corrupted_count = 0

        for blob_hash in self.list_blobs():
            blob_path = self._get_blob_path(blob_hash)
            if blob_path.exists():
                try:
                    content = blob_path.read_bytes()
                    if _verify_hash(content, blob_hash):
                        total_blobs += 1
                        total_bytes += len(content)
                    else:
                        corrupted_count += 1
                except Exception:
                    corrupted_count += 1

        return {
            "total_blobs": total_blobs,
            "total_bytes": total_bytes,
            "corrupted_count": corrupted_count,
            "storage_path": str(self._storage_root),
        }

    def cleanup_corrupted_blobs(self) -> int:
        """Remove corrupted blobs from storage.

        Returns:
            Number of corrupted blobs removed
        """
        removed = 0
        for blob_hash in list(self.list_blobs()):
            blob_path = self._get_blob_path(blob_hash)
            if blob_path.exists():
                try:
                    content = blob_path.read_bytes()
                    if not _verify_hash(content, blob_hash):
                        self.delete_blob(blob_hash)
                        removed += 1
                except Exception:
                    self.delete_blob(blob_hash)
                    removed += 1
        return removed

    def _save_metadata(self, metadata: BlobMetadata) -> None:
        """Save blob metadata to disk.

        Args:
            metadata: Metadata to save
        """
        metadata_path = self._get_metadata_path(metadata.blob_hash)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata.to_dict(), indent=2))

    def _update_metadata(self, blob_hash: str, access_increment: int = 0) -> None:
        """Update metadata for a blob.

        Args:
            blob_hash: Content hash
            access_increment: Amount to increment access count
        """
        metadata = self.get_blob_info(blob_hash)
        if metadata is None:
            return

        metadata.access_count += access_increment
        if access_increment > 0:
            metadata.last_accessed_at = datetime.now(UTC)

        self._save_metadata(metadata)

    def _mark_corrupted(self, blob_hash: str) -> None:
        """Mark a blob as corrupted in metadata.

        Args:
            blob_hash: Content hash
        """
        metadata_path = self._get_metadata_path(blob_hash)
        if metadata_path.exists():
            try:
                data = json.loads(metadata_path.read_text())
                data["corrupted"] = True
                data["corrupted_at"] = datetime.now(UTC).isoformat()
                metadata_path.write_text(json.dumps(data, indent=2))
            except Exception:
                pass


class BlobReference:
    """Reference to a blob in the blob store.

    This class provides a safe way to reference blobs without loading
    the entire content into memory.
    """

    def __init__(
        self,
        blob_hash: str,
        byte_count: int,
        media_type: str,
        blob_store: BlobStore,
    ) -> None:
        """Initialize blob reference.

        Args:
            blob_hash: Content hash
            byte_count: Size in bytes
            media_type: MIME type
            blob_store: Blob store instance
        """
        self.blob_hash = blob_hash
        self.byte_count = byte_count
        self.media_type = media_type
        self._blob_store = blob_store

    def get_content(self) -> bytes:
        """Load blob content from storage.

        Returns:
            Blob content

        Raises:
            ValueError: If blob is not found or corrupted
        """
        content = self._blob_store.get_blob(self.blob_hash)
        if content is None:
            raise ValueError(f"Blob {self.blob_hash} not found")

        return content

    def exists(self) -> bool:
        """Check if blob exists and is valid.

        Returns:
            True if blob exists
        """
        return self._blob_store.has_blob(self.blob_hash)

    def get_info(self) -> BlobMetadata | None:
        """Get blob metadata.

        Returns:
            Blob metadata if available
        """
        return self._blob_store.get_blob_info(self.blob_hash)

    def __repr__(self) -> str:
        """String representation."""
        return f"BlobReference(hash={self.blob_hash[:8]}..., size={self.byte_count}, type={self.media_type})"


class BlobReferenceFactory:
    """Factory for creating blob references.

    This class provides a convenient interface for creating blob
    references and storing content in the blob store.
    """

    def __init__(self, blob_store: BlobStore) -> None:
        """Initialize the factory.

        Args:
            blob_store: Blob store instance
        """
        self._blob_store = blob_store

    def create_reference(
        self,
        content: bytes,
        media_type: str,
        retention_days: int | None = None,
    ) -> BlobReference:
        """Store content and create a blob reference.

        Args:
            content: Binary content
            media_type: MIME type
            retention_days: Optional retention period

        Returns:
            Blob reference

        Raises:
            ValueError: If content is empty
        """
        if not content:
            raise ValueError("Cannot create reference for empty content")

        blob_hash, byte_count = self._blob_store.store_blob(
            content, retention_days=retention_days
        )

        return BlobReference(
            blob_hash=blob_hash,
            byte_count=byte_count,
            media_type=media_type,
            blob_store=self._blob_store,
        )

    def from_existing_hash(
        self,
        blob_hash: str,
        media_type: str,
    ) -> BlobReference | None:
        """Create reference from existing blob hash.

        Args:
            blob_hash: Content hash
            media_type: MIME type

        Returns:
            Blob reference if blob exists, None otherwise
        """
        if not self._blob_store.has_blob(blob_hash):
            return None

        metadata = self._blob_store.get_blob_info(blob_hash)
        if metadata is None:
            return None

        return BlobReference(
            blob_hash=blob_hash,
            byte_count=metadata.byte_count,
            media_type=media_type,
            blob_store=self._blob_store,
        )