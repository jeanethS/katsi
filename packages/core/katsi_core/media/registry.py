"""Authoritative Derived Representation registry.

This module provides the core registry for managing DerivedRepresentation
lifecycles with immutable versions and current-status selection.

Key features:
- Immutable representations with version tracking
- Independent lifecycle transitions (pending/current/partial/unavailable/failed)
- Source resource deletion/change handling
- Current status selection by resource version and pipeline fingerprint
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import override
from uuid import UUID, uuid4

from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    EvidenceLocatorUnion,
    MediaCoverage,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
)
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RepresentationRegistry:
    """Authoritative registry for Derived Representation lifecycles.

    This registry manages immutable representations with independent lifecycle
    transitions. Each representation is versioned and can be retrieved by
    current status, source resource, or pipeline fingerprint.

    The registry preserves historical representations while removing
    non-current projection visibility when source resources change.
    """

    def __init__(self, database: WorkspaceSQLite) -> None:
        """Initialize the registry with a database connection.

        Args:
            database: WorkspaceSQLite database connection
        """
        self._database = database
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the representations table if it doesn't exist."""
        with self._database.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS representations (
                    id TEXT PRIMARY KEY,
                    resource_version_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    textual_payload TEXT,
                    blob_reference TEXT,
                    blob_hash TEXT,
                    blob_byte_count INTEGER,
                    locators TEXT NOT NULL,
                    coverage_fraction REAL NOT NULL,
                    coverage_is_complete INTEGER NOT NULL,
                    coverage_detail TEXT NOT NULL,
                    confidence REAL,
                    producer_type TEXT NOT NULL,
                    adapter_name TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    model_identity TEXT,
                    model_version TEXT,
                    error_category TEXT,
                    error_message TEXT,
                    error_is_retriable INTEGER,
                    error_diagnostic TEXT,
                    pipeline_fingerprint TEXT NOT NULL,
                    is_current INTEGER DEFAULT 1,
                    INDEX (resource_version_id, kind, status),
                    INDEX (blob_hash),
                    INDEX (pipeline_fingerprint)
                )
            """)

    def register_representation(
        self,
        representation: DerivedRepresentation,
        make_current: bool = True,
    ) -> None:
        """Register a new immutable representation.

        Args:
            representation: The representation to register
            make_current: Whether to mark this representation as current

        Raises:
            ValueError: If representation validation fails
        """
        # Validate representation before storage
        representation.model_validate(representation.model_dump())

        timestamp = _utc_now()

        with self._database.connection() as conn:
            # If making current, mark existing current representations as non-current
            if make_current and representation.status == MediaRepresentationStatus.CURRENT:
                conn.execute("""
                    UPDATE representations
                    SET is_current = 0, updated_at = ?
                    WHERE resource_version_id = ?
                      AND kind = ?
                      AND is_current = 1
                """, (timestamp.isoformat(), str(representation.resource_version_id), representation.kind.value))

            # Serialize complex fields
            locators_json = json.dumps([loc.model_dump() for loc in representation.locators])
            pipeline_fingerprint_json = json.dumps(representation.pipeline_fingerprint.model_dump())

            # Insert the new representation
            conn.execute("""
                INSERT INTO representations (
                    id, resource_version_id, kind, media_type, status,
                    created_at, updated_at, textual_payload, blob_reference,
                    blob_hash, blob_byte_count, locators, coverage_fraction,
                    coverage_is_complete, coverage_detail, confidence,
                    producer_type, adapter_name, adapter_version, model_identity,
                    model_version, error_category, error_message,
                    error_is_retriable, error_diagnostic, pipeline_fingerprint,
                    is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(representation.id),
                str(representation.resource_version_id),
                representation.kind.value,
                representation.media_type,
                representation.status.value,
                representation.created_at.isoformat(),
                representation.updated_at.isoformat(),
                representation.textual_payload,
                representation.blob_reference,
                str(representation.blob_hash) if representation.blob_hash else None,
                representation.blob_byte_count,
                locators_json,
                representation.coverage.coverage_fraction,
                1 if representation.coverage.is_complete else 0,
                representation.coverage.detail,
                representation.confidence,
                representation.producer.producer_type.value,
                representation.producer.adapter_name,
                representation.producer.adapter_version,
                representation.producer.model_identity,
                representation.producer.model_version,
                representation.error.error_category if representation.error else None,
                representation.error.error_message if representation.error else None,
                1 if representation.error and representation.error.is_retriable else 0,
                json.dumps(representation.error.diagnostic_info) if representation.error else None,
                pipeline_fingerprint_json,
                1 if make_current else 0,
            ))

    def get_representation(self, representation_id: UUID) -> DerivedRepresentation | None:
        """Get a representation by its ID.

        Args:
            representation_id: UUID of the representation

        Returns:
            The representation if found, None otherwise
        """
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM representations WHERE id = ?",
                (str(representation_id),)
            ).fetchone()

            if row is None:
                return None

            return self._row_to_representation(row)

    def get_current_representation(
        self,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
    ) -> DerivedRepresentation | None:
        """Get the current representation for a resource and kind.

        Args:
            resource_version_id: Source resource version
            kind: Representation kind

        Returns:
            The current representation if found, None otherwise
        """
        with self._database.connection() as conn:
            row = conn.execute("""
                SELECT * FROM representations
                WHERE resource_version_id = ?
                  AND kind = ?
                  AND status = ?
                  AND is_current = 1
                ORDER BY created_at DESC
                LIMIT 1
            """, (str(resource_version_id), kind.value, MediaRepresentationStatus.CURRENT.value)).fetchone()

            if row is None:
                return None

            return self._row_to_representation(row)

    def get_representations_by_resource(
        self,
        resource_version_id: ResourceVersionId,
        status: MediaRepresentationStatus | None = None,
    ) -> list[DerivedRepresentation]:
        """Get all representations for a resource, optionally filtered by status.

        Args:
            resource_version_id: Source resource version
            status: Optional status filter

        Returns:
            List of representations matching the criteria
        """
        with self._database.connection() as conn:
            if status is None:
                rows = conn.execute("""
                    SELECT * FROM representations
                    WHERE resource_version_id = ?
                    ORDER BY created_at DESC
                """, (str(resource_version_id),)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM representations
                    WHERE resource_version_id = ? AND status = ?
                    ORDER BY created_at DESC
                """, (str(resource_version_id), status.value)).fetchall()

            return [self._row_to_representation(row) for row in rows]

    def get_representations_by_pipeline(
        self,
        pipeline_fingerprint: PipelineFingerprint,
    ) -> list[DerivedRepresentation]:
        """Get representations with a specific pipeline fingerprint.

        Args:
            pipeline_fingerprint: Pipeline fingerprint to match

        Returns:
            List of representations with matching pipeline fingerprint
        """
        with self._database.connection() as conn:
            fingerprint_json = json.dumps(pipeline_fingerprint.model_dump())
            rows = conn.execute("""
                SELECT * FROM representations
                WHERE pipeline_fingerprint = ?
                ORDER BY created_at DESC
            """, (fingerprint_json,)).fetchall()

            return [self._row_to_representation(row) for row in rows]

    def find_cached_representation(
        self,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
        pipeline_fingerprint: PipelineFingerprint,
    ) -> DerivedRepresentation | None:
        """Find a cached representation with matching pipeline fingerprint.

        Args:
            resource_version_id: Source resource version
            kind: Representation kind
            pipeline_fingerprint: Pipeline fingerprint to match

        Returns:
            Cached representation if found, None otherwise
        """
        with self._database.connection() as conn:
            fingerprint_json = json.dumps(pipeline_fingerprint.model_dump())
            row = conn.execute("""
                SELECT * FROM representations
                WHERE resource_version_id = ?
                  AND kind = ?
                  AND pipeline_fingerprint = ?
                  AND status IN (?, ?)
                ORDER BY created_at DESC
                LIMIT 1
            """, (
                str(resource_version_id),
                kind.value,
                fingerprint_json,
                MediaRepresentationStatus.CURRENT.value,
                MediaRepresentationStatus.PARTIAL.value,
            )).fetchone()

            if row is None:
                return None

            return self._row_to_representation(row)

    def update_representation_status(
        self,
        representation_id: UUID,
        new_status: MediaRepresentationStatus,
        error: RepresentationError | None = None,
    ) -> None:
        """Update the status of a representation.

        Args:
            representation_id: UUID of the representation to update
            new_status: New status
            error: Optional error information for failed/unavailable status

        Raises:
            ValueError: If representation not found or status transition invalid
        """
        timestamp = _utc_now()

        with self._database.connection() as conn:
            # Check representation exists
            existing = conn.execute(
                "SELECT status FROM representations WHERE id = ?",
                (str(representation_id),)
            ).fetchone()

            if existing is None:
                raise ValueError(f"Representation {representation_id} not found")

            # Update status
            error_category = error.error_category if error else None
            error_message = error.error_message if error else None
            error_is_retriable = 1 if error and error.is_retriable else 0
            error_diagnostic = json.dumps(error.diagnostic_info) if error else None

            conn.execute("""
                UPDATE representations
                SET status = ?, updated_at = ?,
                    error_category = ?, error_message = ?,
                    error_is_retriable = ?, error_diagnostic = ?
                WHERE id = ?
            """, (
                new_status.value,
                timestamp.isoformat(),
                error_category,
                error_message,
                error_is_retriable,
                error_diagnostic,
                str(representation_id),
            ))

    def handle_resource_deletion(
        self,
        resource_version_id: ResourceVersionId,
        preserve_historical: bool = True,
    ) -> None:
        """Handle source resource deletion/change.

        This preserves historical representations while removing non-current
        projection visibility. When preserve_historical is False, all
        representations are deleted.

        Args:
            resource_version_id: Deleted resource version
            preserve_historical: Whether to preserve historical representations
        """
        with self._database.connection() as conn:
            if preserve_historical:
                # Mark all as non-current but preserve historical records
                conn.execute("""
                    UPDATE representations
                    SET is_current = 0, updated_at = ?
                    WHERE resource_version_id = ?
                """, (_utc_now().isoformat(), str(resource_version_id)))
            else:
                # Delete all representations for this resource
                conn.execute("""
                    DELETE FROM representations
                    WHERE resource_version_id = ?
                """, (str(resource_version_id),))

    def _row_to_representation(self, row: tuple) -> DerivedRepresentation:
        """Convert a database row to a DerivedRepresentation.

        Args:
            row: Database row

        Returns:
            DerivedRepresentation instance
        """
        # Unpack row (this order matches the CREATE TABLE statement)
        (
            rep_id, resource_version_id, kind, media_type, status,
            created_at_str, updated_at_str, textual_payload, blob_reference,
            blob_hash_str, blob_byte_count, locators_json,
            coverage_fraction, coverage_is_complete, coverage_detail,
            confidence, producer_type, adapter_name, adapter_version,
            model_identity, model_version, error_category, error_message,
            error_is_retriable, error_diagnostic_json, pipeline_fingerprint_json,
            is_current,
        ) = row

        # Parse complex fields
        locators_data = json.loads(locators_json)
        locators = tuple[EvidenceLocatorUnion, ...](locators_data)  # type: ignore

        coverage = MediaCoverage(
            is_complete=bool(coverage_is_complete),
            coverage_fraction=coverage_fraction,
            detail=coverage_detail,
        )

        producer = ProducerProvenance(
            producer_type=producer_type,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            model_identity=model_identity,
            model_version=model_version,
        )

        pipeline_fingerprint_data = json.loads(pipeline_fingerprint_json)
        pipeline_fingerprint = PipelineFingerprint(**pipeline_fingerprint_data)

        error = None
        if error_category is not None:
            error = RepresentationError(
                error_category=error_category,
                error_message=error_message,
                is_retriable=bool(error_is_retriable),
                diagnostic_info=json.loads(error_diagnostic_json) if error_diagnostic_json else {},
            )

        return DerivedRepresentation(
            id=UUID(rep_id),
            resource_version_id=ResourceVersionId(resource_version_id),
            kind=MediaRepresentationKind(kind),
            media_type=media_type,
            status=MediaRepresentationStatus(status),
            created_at=datetime.fromisoformat(created_at_str),
            updated_at=datetime.fromisoformat(updated_at_str),
            textual_payload=textual_payload,
            blob_reference=blob_reference,
            blob_hash=ContentHash(blob_hash_str) if blob_hash_str else None,
            blob_byte_count=blob_byte_count,
            locators=locators,
            coverage=coverage,
            confidence=confidence,
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
            error=error,
        )

    def cleanup_stale_blobs(self, blob_store: "BlobStore", max_age_days: int = 30) -> int:
        """Clean up unreferenced blobs from the blob store.

        Args:
            blob_store: Blob store instance to clean
            max_age_days: Maximum age of unused blobs to keep

        Returns:
            Number of blobs cleaned up
        """
        # Get all blob hashes referenced by current representations
        with self._database.connection() as conn:
            referenced_hashes = set(conn.execute("""
                SELECT DISTINCT blob_hash
                FROM representations
                WHERE blob_hash IS NOT NULL
                  AND is_current = 1
            """).fetchall())

        # Convert from tuples to strings
        referenced_hashes = {row[0] for row in referenced_hashes}

        # Get all blob hashes from blob store
        all_blobs = blob_store.list_blobs()
        unreferenced = all_blobs - referenced_hashes

        # Clean up unreferenced blobs
        cleaned = 0
        for blob_hash in unreferenced:
            if blob_store.can_cleanup(blob_hash, max_age_days):
                blob_store.delete_blob(blob_hash)
                cleaned += 1

        return cleaned


class RepresentationLifecycleManager:
    """Manager for representation lifecycle transitions.

    This class handles the transition rules for representations between
    pending, current, partial, unavailable, and failed states.
    """

    def __init__(self, registry: RepresentationRegistry) -> None:
        """Initialize the lifecycle manager.

        Args:
            registry: Representation registry instance
        """
        self._registry = registry

    def create_pending_representation(
        self,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
        media_type: str,
        producer: ProducerProvenance,
        pipeline_fingerprint: PipelineFingerprint,
        locators: tuple[EvidenceLocatorUnion, ...] = (),
    ) -> DerivedRepresentation:
        """Create a new pending representation.

        Args:
            resource_version_id: Source resource version
            kind: Representation kind
            media_type: MIME type of representation
            producer: Producer information
            pipeline_fingerprint: Pipeline fingerprint
            locators: Evidence locators

        Returns:
            Created pending representation
        """
        timestamp = _utc_now()

        representation = DerivedRepresentation(
            id=uuid4(),
            resource_version_id=resource_version_id,
            kind=kind,
            media_type=media_type,
            status=MediaRepresentationStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
            locators=locators,
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
            producer=producer,
            pipeline_fingerprint=pipeline_fingerprint,
        )

        self._registry.register_representation(representation, make_current=False)
        return representation

    def transition_to_current(
        self,
        representation_id: UUID,
        textual_payload: str | None = None,
        blob_reference: str | None = None,
        blob_hash: ContentHash | None = None,
        blob_byte_count: int | None = None,
        coverage: MediaCoverage | None = None,
        confidence: float | None = None,
    ) -> DerivedRepresentation:
        """Transition a pending representation to current status.

        Args:
            representation_id: UUID of the pending representation
            textual_payload: Text content for text-based representations
            blob_reference: Blob reference for binary representations
            blob_hash: Hash of blob content
            blob_byte_count: Size of blob in bytes
            coverage: Coverage information
            confidence: Confidence score

        Returns:
            Updated current representation

        Raises:
            ValueError: If representation not found or invalid transition
        """
        # Get existing representation
        representation = self._registry.get_representation(representation_id)
        if representation is None:
            raise ValueError(f"Representation {representation_id} not found")

        if representation.status != MediaRepresentationStatus.PENDING:
            raise ValueError(f"Representation {representation_id} is not pending")

        # Create new immutable version with current status
        timestamp = _utc_now()

        updated = DerivedRepresentation(
            id=representation.id,  # Keep same ID for immutability within version
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.CURRENT,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=textual_payload or representation.textual_payload,
            blob_reference=blob_reference or representation.blob_reference,
            blob_hash=blob_hash or representation.blob_hash,
            blob_byte_count=blob_byte_count or representation.blob_byte_count,
            locators=representation.locators,
            coverage=coverage or MediaCoverage(is_complete=True, coverage_fraction=1.0),
            confidence=confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
        )

        # Update in registry (this will mark as current)
        self._registry.register_representation(updated, make_current=True)
        return updated

    def transition_to_partial(
        self,
        representation_id: UUID,
        coverage: MediaCoverage,
        textual_payload: str | None = None,
        blob_reference: str | None = None,
        blob_hash: ContentHash | None = None,
        blob_byte_count: int | None = None,
    ) -> DerivedRepresentation:
        """Transition a pending representation to partial status.

        Args:
            representation_id: UUID of the pending representation
            coverage: Coverage information
            textual_payload: Partial text content
            blob_reference: Partial blob reference
            blob_hash: Hash of partial blob
            blob_byte_count: Size of partial blob

        Returns:
            Updated partial representation

        Raises:
            ValueError: If representation not found or invalid transition
        """
        representation = self._registry.get_representation(representation_id)
        if representation is None:
            raise ValueError(f"Representation {representation_id} not found")

        if representation.status != MediaRepresentationStatus.PENDING:
            raise ValueError(f"Representation {representation_id} is not pending")

        timestamp = _utc_now()

        updated = DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.PARTIAL,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=textual_payload or representation.textual_payload,
            blob_reference=blob_reference or representation.blob_reference,
            blob_hash=blob_hash or representation.blob_hash,
            blob_byte_count=blob_byte_count or representation.blob_byte_count,
            locators=representation.locators,
            coverage=coverage,
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
        )

        self._registry.register_representation(updated, make_current=False)
        return updated

    def transition_to_unavailable(
        self,
        representation_id: UUID,
        error: RepresentationError,
    ) -> DerivedRepresentation:
        """Transition a pending representation to unavailable status.

        Args:
            representation_id: UUID of the pending representation
            error: Error details

        Returns:
            Updated unavailable representation

        Raises:
            ValueError: If representation not found or invalid transition
        """
        representation = self._registry.get_representation(representation_id)
        if representation is None:
            raise ValueError(f"Representation {representation_id} not found")

        timestamp = _utc_now()

        updated = DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.UNAVAILABLE,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=representation.textual_payload,
            blob_reference=representation.blob_reference,
            blob_hash=representation.blob_hash,
            blob_byte_count=representation.blob_byte_count,
            locators=representation.locators,
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
            error=error,
        )

        self._registry.register_representation(updated, make_current=False)
        return updated

    def transition_to_failed(
        self,
        representation_id: UUID,
        error: RepresentationError,
    ) -> DerivedRepresentation:
        """Transition a pending representation to failed status.

        Args:
            representation_id: UUID of the pending representation
            error: Error details

        Returns:
            Updated failed representation

        Raises:
            ValueError: If representation not found or invalid transition
        """
        representation = self._registry.get_representation(representation_id)
        if representation is None:
            raise ValueError(f"Representation {representation_id} not found")

        timestamp = _utc_now()

        updated = DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.FAILED,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=representation.textual_payload,
            blob_reference=representation.blob_reference,
            blob_hash=representation.blob_hash,
            blob_byte_count=representation.blob_byte_count,
            locators=representation.locators,
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
            error=error,
        )

        self._registry.register_representation(updated, make_current=False)
        return updated