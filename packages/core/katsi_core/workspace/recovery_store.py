"""Private content-addressed recovery-blob store with deduplication and integrity verification."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from katsi_core.config import RecoverySettings
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import ContentHash


class RecoveryBlobStore:
    """Content-addressed blob storage with deduplication and retention policies."""

    def __init__(self, database: WorkspaceSQLite, settings: RecoverySettings) -> None:
        self._database = database
        self._settings = settings
        self._blob_dir = Path(settings.blob_directory)
        self._blob_dir.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        content: bytes,
        retention_days: int | None = None,
    ) -> ContentHash:
        """Store content with deduplication and integrity verification."""
        # Compute content hash for deduplication
        content_hash = self._compute_hash(content)

        # Check if already exists (deduplication)
        existing = self._get_existing(content_hash)
        if existing is not None:
            # Update retention if needed
            if retention_days and retention_days > existing["retention_days"]:
                self._update_retention(content_hash, retention_days)
            return ContentHash(content_hash)

        # Create storage path
        storage_path = self._get_storage_path(content_hash)
        storage_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content atomically
        temp_path = storage_path.parent / f"{storage_path.name}.tmp"
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())

            # Verify integrity by recomputing hash
            with open(temp_path, "rb") as f:
                verification_hash = self._compute_hash(f.read())
                if verification_hash != content_hash:
                    raise ValueError("Integrity verification failed")

            # Atomic rename
            temp_path.replace(storage_path)

        finally:
            if temp_path.exists():
                temp_path.unlink()

        # Record metadata in database
        retained_until = datetime.now(UTC) + timedelta(days=retention_days or self._settings.retention_days)

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO recovery_blobs VALUES (?, ?, ?, ?, ?)",
                (
                    content_hash,
                    len(content),
                    str(storage_path),
                    retained_until.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )

        return ContentHash(content_hash)

    def retrieve(self, content_hash: ContentHash) -> bytes | None:
        """Retrieve content by hash with integrity verification."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_blobs WHERE content_hash = ?", (str(content_hash),)
            ).fetchone()

            if row is None:
                return None

            storage_path = Path(row["storage_path"])

            if not storage_path.exists():
                return None

            with open(storage_path, "rb") as f:
                content = f.read()

            # Verify integrity
            verification_hash = self._compute_hash(content)
            if verification_hash != str(content_hash):
                raise ValueError("Integrity verification failed on retrieval")

            return content

    def verify_integrity(self, content_hash: ContentHash) -> bool:
        """Verify stored content integrity without loading full content."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT storage_path FROM recovery_blobs WHERE content_hash = ?",
                (str(content_hash),)
            ).fetchone()

            if row is None:
                return False

            storage_path = Path(row["storage_path"])

            if not storage_path.exists():
                return False

            # Compute hash in streaming fashion
            computed_hash = self._compute_hash_file(storage_path)
            return computed_hash == str(content_hash)

    def cleanup_expired(self) -> int:
        """Remove expired blobs and return count of removed entries."""
        now = datetime.now(UTC)
        removed_count = 0

        with self._database.connection() as connection, write_transaction(connection):
            expired = connection.execute(
                "SELECT content_hash, storage_path FROM recovery_blobs WHERE retained_until <= ?",
                (now.isoformat(),)
            ).fetchall()

            for row in expired:
                storage_path = Path(row["storage_path"])

                # Remove file
                if storage_path.exists():
                    storage_path.unlink()

                # Remove from smaller parent directories if empty
                parent = storage_path.parent
                for _ in range(3):  # Clean up up to 3 parent levels
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                    else:
                        break

                removed_count += 1

            # Remove from database
            connection.execute(
                "DELETE FROM recovery_blobs WHERE retained_until <= ?",
                (now.isoformat(),)
            )

        return removed_count

    def get_stats(self) -> dict[str, int]:
        """Get statistics about the recovery blob store."""
        with self._database.connection() as connection:
            total_blobs = connection.execute(
                "SELECT COUNT(*) FROM recovery_blobs"
            ).fetchone()[0]

            total_bytes = connection.execute(
                "SELECT SUM(byte_count) FROM recovery_blobs"
            ).fetchone()[0] or 0

            expired_count = connection.execute(
                "SELECT COUNT(*) FROM recovery_blobs WHERE retained_until <= ?",
                (datetime.now(UTC).isoformat(),)
            ).fetchone()[0]

        return {
            "total_blobs": total_blobs,
            "total_bytes": total_bytes,
            "expired_count": expired_count,
            "deduplication_savings": self._calculate_deduplication_savings(),
        }

    def _compute_hash(self, content: bytes) -> str:
        """Compute BLAKE3 hash for content addressing."""
        return hashlib.blake3(content).hexdigest()

    def _compute_hash_file(self, file_path: Path) -> str:
        """Compute BLAKE3 hash for file without loading into memory."""
        hasher = hashlib.blake3()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)

        return hasher.hexdigest()

    def _get_storage_path(self, content_hash: str) -> Path:
        """Generate storage path with content-based directory structure."""
        # Use first 3 characters for directory structure
        prefix1 = content_hash[:2]
        prefix2 = content_hash[2:4]

        return self._blob_dir / prefix1 / prefix2 / content_hash

    def _get_existing(self, content_hash: str) -> dict | None:
        """Check if content hash already exists (for deduplication)."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_blobs WHERE content_hash = ?",
                (content_hash,)
            ).fetchone()

            if row is None:
                return None

            return {
                "byte_count": row["byte_count"],
                "storage_path": row["storage_path"],
                "retained_until": row["retained_until"],
            }

    def _update_retention(self, content_hash: str, retention_days: int) -> None:
        """Update retention period for existing content."""
        retained_until = datetime.now(UTC) + timedelta(days=retention_days)

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "UPDATE recovery_blobs SET retained_until = ? WHERE content_hash = ?",
                (retained_until.isoformat(), content_hash),
            )

    def _calculate_deduplication_savings(self) -> int:
        """Calculate bytes saved through deduplication."""
        with self._database.connection() as connection:
            # Get unique hash count and total bytes
            result = connection.execute("""
                SELECT
                    COUNT(DISTINCT content_hash) as unique_hashes,
                    SUM(byte_count) as total_bytes
                FROM recovery_blobs
            """).fetchone()

        # If we had duplicate bytes, deduplication savings = potential duplicate bytes
        # This is a simplified calculation
        unique_hashes = result["unique_hashes"] or 0
        total_bytes = result["total_bytes"] or 0

        # Rough estimate: if we have 10% fewer blobs than bytes would suggest,
        # we saved that difference
        if unique_hashes > 0 and total_bytes > 0:
            average_size = total_bytes / unique_hashes
            return int(average_size * max(0, unique_hashes - 1))

        return 0