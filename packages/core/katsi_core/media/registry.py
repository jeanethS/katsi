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
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from katsi_core.media.blob_store import BlobStore

from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    EvidenceLocatorUnion,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
)
from katsi_core.media.fingerprint import fingerprint_digest
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _fingerprint_from_json(pipeline_fingerprint_json: str) -> PipelineFingerprint:
    """Rebuild a stored fingerprint; StrictModel does not coerce strings."""
    data = json.loads(pipeline_fingerprint_json)
    data["representation_kind"] = MediaRepresentationKind(data["representation_kind"])
    data["stage"] = PipelineStage(data["stage"])
    if data.get("input_representation_id") is not None:
        data["input_representation_id"] = UUID(data["input_representation_id"])
    return PipelineFingerprint(**data)


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
                    fingerprint_digest TEXT,
                    is_current INTEGER DEFAULT 1
                )
            """)

            self._ensure_fingerprint_digest_column(conn)

            # Create indexes for better query performance
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_representations_resource_kind_status
                ON representations (resource_version_id, kind, status)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_representations_blob_hash
                ON representations (blob_hash)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_representations_pipeline_fingerprint
                ON representations (pipeline_fingerprint)
            """)

            # Compatible-cache lookup: without this index every cache miss
            # scanned and hydrated every representation of the kind, making a
            # whole-library reprocess quadratic in the number of media files.
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_representations_fingerprint_digest
                ON representations (kind, fingerprint_digest, status)
            """)

    def _ensure_fingerprint_digest_column(self, conn: Any) -> None:
        """Add and backfill ``fingerprint_digest`` for databases created before it."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(representations)").fetchall()}
        if "fingerprint_digest" in columns:
            return
        conn.execute("ALTER TABLE representations ADD COLUMN fingerprint_digest TEXT")
        rows = conn.execute("SELECT id, pipeline_fingerprint FROM representations").fetchall()
        conn.executemany(
            "UPDATE representations SET fingerprint_digest = ? WHERE id = ?",
            [(fingerprint_digest(_fingerprint_from_json(row[1])), row[0]) for row in rows],
        )

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

        with self._database.connection() as conn:
            # If making current, mark existing current representations as non-current
            if make_current and representation.status == MediaRepresentationStatus.CURRENT:
                self._supersede_current_generation(
                    conn, representation.resource_version_id, representation.kind
                )
            self._insert_representation(conn, representation, make_current)

    def register_representation_batch(
        self,
        representations: list[DerivedRepresentation],
        make_current: bool = True,
    ) -> None:
        """Register N representations of one kind as a single current generation.

        Scenes, keyframes, transcript segments and silence spans are inherently
        many-per-kind: one media file yields N of them. Registering those one at
        a time through :meth:`register_representation` would make each member
        supersede the one before it, leaving only the last visible. Supersession
        is a property of a *generation* of output, not of an individual row, so
        this retires the previous generation exactly once and then admits every
        member of the new one.

        All members must share one ``resource_version_id`` and one ``kind``;
        that is what makes "the previous generation" well defined. An empty
        list is a no-op.

        Raises:
            ValueError: If the batch spans multiple resources or kinds.
        """
        if not representations:
            return

        resource_version_ids = {str(item.resource_version_id) for item in representations}
        if len(resource_version_ids) != 1:
            raise ValueError("A batch must share a single resource_version_id")
        kinds = {item.kind for item in representations}
        if len(kinds) != 1:
            raise ValueError("A batch must share a single representation kind")

        for item in representations:
            item.model_validate(item.model_dump())

        first = representations[0]
        with self._database.connection() as conn:
            if make_current and any(
                item.status
                in {MediaRepresentationStatus.CURRENT, MediaRepresentationStatus.PARTIAL}
                for item in representations
            ):
                self._supersede_current_generation(conn, first.resource_version_id, first.kind)
            for item in representations:
                self._insert_representation(conn, item, make_current)

    def _supersede_current_generation(
        self,
        conn: Any,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
    ) -> None:
        """Retire the currently-visible generation of one kind for one resource."""
        conn.execute(
            """
            UPDATE representations
            SET is_current = 0, updated_at = ?
            WHERE resource_version_id = ?
              AND kind = ?
              AND is_current = 1
        """,
            (
                _utc_now().isoformat(),
                str(resource_version_id),
                kind.value,
            ),
        )

    def _insert_representation(
        self,
        conn: Any,
        representation: DerivedRepresentation,
        make_current: bool,
    ) -> None:
        """Insert one immutable representation row.

        Shared by the single and batch registration paths so the column list
        cannot drift between them.
        """
        # Serialize complex fields
        locators_json = json.dumps([loc.model_dump(mode="json") for loc in representation.locators])
        pipeline_fingerprint_json = json.dumps(
            representation.pipeline_fingerprint.model_dump(mode="json")
        )

        # Insert the new representation
        conn.execute(
            """
            INSERT INTO representations (
                id, resource_version_id, kind, media_type, status,
                created_at, updated_at, textual_payload, blob_reference,
                blob_hash, blob_byte_count, locators, coverage_fraction,
                coverage_is_complete, coverage_detail, confidence,
                producer_type, adapter_name, adapter_version, model_identity,
                model_version, error_category, error_message,
                error_is_retriable, error_diagnostic, pipeline_fingerprint,
                fingerprint_digest, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
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
                fingerprint_digest(representation.pipeline_fingerprint),
                1 if make_current else 0,
            ),
        )

    def count_current_resources_by_status(self, kind: MediaRepresentationKind) -> dict[str, int]:
        """Count distinct current resource versions of ``kind``, grouped by status."""
        with self._database.connection() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(DISTINCT resource_version_id) AS count
                FROM representations
                WHERE kind = ? AND is_current = 1
                GROUP BY status
                """,
                (kind.value,),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def find_by_fingerprint_digest(
        self,
        kind: MediaRepresentationKind,
        digest: str,
        statuses: tuple[MediaRepresentationStatus, ...],
    ) -> DerivedRepresentation | None:
        """Find the newest representation of ``kind`` with a matching cache digest."""
        placeholders = ",".join("?" for _ in statuses)
        with self._database.connection() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM representations
                WHERE kind = ?
                  AND fingerprint_digest = ?
                  AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1
                """,  # noqa: S608 - placeholders are generated, values stay bound
                (kind.value, digest, *(status.value for status in statuses)),
            ).fetchone()
        return self._row_to_representation(row) if row is not None else None

    def get_representation(self, representation_id: UUID) -> DerivedRepresentation | None:
        """Get a representation by its ID.

        Args:
            representation_id: UUID of the representation

        Returns:
            The representation if found, None otherwise
        """
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM representations WHERE id = ?", (str(representation_id),)
            ).fetchone()

            if row is None:
                return None

            return self._row_to_representation(row)

    def is_current(self, representation_id: UUID) -> bool:
        """Whether this immutable representation is currently visible.

        Status alone is insufficient because a newer representation of the
        same kind can supersede it while its historical status remains current.
        """
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT status, is_current FROM representations WHERE id = ?",
                (str(representation_id),),
            ).fetchone()
        return bool(
            row
            and row["is_current"]
            and row["status"]
            in {
                MediaRepresentationStatus.CURRENT.value,
                MediaRepresentationStatus.PARTIAL.value,
            }
        )

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
            row = conn.execute(
                """
                SELECT * FROM representations
                WHERE resource_version_id = ?
                  AND kind = ?
                  AND status = ?
                  AND is_current = 1
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (str(resource_version_id), kind.value, MediaRepresentationStatus.CURRENT.value),
            ).fetchone()

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
                rows = conn.execute(
                    """
                    SELECT * FROM representations
                    WHERE resource_version_id = ?
                    ORDER BY created_at DESC
                """,
                    (str(resource_version_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM representations
                    WHERE resource_version_id = ? AND status = ?
                    ORDER BY created_at DESC
                """,
                    (str(resource_version_id), status.value),
                ).fetchall()

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
            fingerprint_json = json.dumps(pipeline_fingerprint.model_dump(mode="json"))
            rows = conn.execute(
                """
                SELECT * FROM representations
                WHERE pipeline_fingerprint = ?
                ORDER BY created_at DESC
            """,
                (fingerprint_json,),
            ).fetchall()

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
            fingerprint_json = json.dumps(pipeline_fingerprint.model_dump(mode="json"))
            row = conn.execute(
                """
                SELECT * FROM representations
                WHERE resource_version_id = ?
                  AND kind = ?
                  AND pipeline_fingerprint = ?
                  AND status IN (?, ?)
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (
                    str(resource_version_id),
                    kind.value,
                    fingerprint_json,
                    MediaRepresentationStatus.CURRENT.value,
                    MediaRepresentationStatus.PARTIAL.value,
                ),
            ).fetchone()

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
                "SELECT status FROM representations WHERE id = ?", (str(representation_id),)
            ).fetchone()

            if existing is None:
                raise ValueError(f"Representation {representation_id} not found")

            # Update status
            error_category = error.error_category if error else None
            error_message = error.error_message if error else None
            error_is_retriable = 1 if error and error.is_retriable else 0
            error_diagnostic = json.dumps(error.diagnostic_info) if error else None

            conn.execute(
                """
                UPDATE representations
                SET status = ?, updated_at = ?,
                    error_category = ?, error_message = ?,
                    error_is_retriable = ?, error_diagnostic = ?
                WHERE id = ?
            """,
                (
                    new_status.value,
                    timestamp.isoformat(),
                    error_category,
                    error_message,
                    error_is_retriable,
                    error_diagnostic,
                    str(representation_id),
                ),
            )

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
                conn.execute(
                    """
                    UPDATE representations
                    SET is_current = 0, updated_at = ?
                    WHERE resource_version_id = ?
                """,
                    (_utc_now().isoformat(), str(resource_version_id)),
                )
            else:
                # Delete all representations for this resource
                conn.execute(
                    """
                    DELETE FROM representations
                    WHERE resource_version_id = ?
                """,
                    (str(resource_version_id),),
                )

    def _row_to_representation(self, row: tuple) -> DerivedRepresentation:
        """Convert a database row to a DerivedRepresentation.

        Args:
            row: Database row

        Returns:
            DerivedRepresentation instance
        """
        # Unpack row (this order matches the CREATE TABLE statement)
        (
            rep_id,
            resource_version_id,
            kind,
            media_type,
            status,
            created_at_str,
            updated_at_str,
            textual_payload,
            blob_reference,
            blob_hash_str,
            blob_byte_count,
            locators_json,
            coverage_fraction,
            coverage_is_complete,
            coverage_detail,
            confidence,
            producer_type,
            adapter_name,
            adapter_version,
            model_identity,
            model_version,
            error_category,
            error_message,
            error_is_retriable,
            error_diagnostic_json,
            pipeline_fingerprint_json,
            # is_current and fingerprint_digest are derived storage columns, and
            # their order differs between fresh and migrated databases.
            *_derived,
        ) = row

        # Parse complex fields
        locators_data = json.loads(locators_json)
        # Convert locator dicts back to proper locator types using the discriminator
        parsed_locators = []
        for loc_data in locators_data:
            # Convert UUIDs back to proper types
            loc_data["resource_version_id"] = ResourceVersionId(loc_data["resource_version_id"])
            loc_data["representation_id"] = UUID(loc_data["representation_id"])

            # Use the discriminator field to create the correct locator type
            locator_type = loc_data.get("locator_type")
            if locator_type == "whole_resource":
                from katsi_core.media.contracts import WholeResourceLocator

                parsed_locators.append(WholeResourceLocator(**loc_data))
            elif locator_type == "text_range":
                from katsi_core.media.contracts import TextRangeLocator

                parsed_locators.append(TextRangeLocator(**loc_data))
            elif locator_type == "page":
                from katsi_core.media.contracts import PageLocator

                parsed_locators.append(PageLocator(**loc_data))
            elif locator_type == "image_region":
                from katsi_core.media.contracts import ImageRegionLocator

                # JSON storage round-trips the tuple as a list; coerce at the
                # deserialization boundary so the in-memory contract holds.
                box = loc_data.get("bounding_box")
                if isinstance(box, list):
                    loc_data = {**loc_data, "bounding_box": tuple(box)}
                parsed_locators.append(ImageRegionLocator(**loc_data))
            elif locator_type == "time_range":
                from katsi_core.media.contracts import TimeRangeLocator

                parsed_locators.append(TimeRangeLocator(**loc_data))
            elif locator_type == "video_frame":
                from katsi_core.media.contracts import VideoFrameLocator

                parsed_locators.append(VideoFrameLocator(**loc_data))
            elif locator_type == "scene":
                from katsi_core.media.contracts import SceneLocator

                loc_data["keyframe_ids"] = tuple(loc_data.get("keyframe_ids", ()))
                parsed_locators.append(SceneLocator(**loc_data))
            else:
                # Fallback for unknown types
                parsed_locators.append(loc_data)

        locators = tuple[EvidenceLocatorUnion, ...](parsed_locators)  # type: ignore

        coverage = MediaCoverage(
            is_complete=bool(coverage_is_complete),
            coverage_fraction=coverage_fraction,
            detail=coverage_detail,
        )

        producer = ProducerProvenance(
            producer_type=MediaProducerType(producer_type),
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            model_identity=model_identity,
            model_version=model_version,
        )

        pipeline_fingerprint = _fingerprint_from_json(pipeline_fingerprint_json)

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

    def cleanup_stale_blobs(self, blob_store: BlobStore, max_age_days: int = 30) -> int:
        """Clean up unreferenced blobs from the blob store.

        Args:
            blob_store: Blob store instance to clean
            max_age_days: Maximum age of unused blobs to keep

        Returns:
            Number of blobs cleaned up
        """
        # Get all blob hashes referenced by current representations
        with self._database.connection() as conn:
            referenced_hashes = set(
                conn.execute("""
                SELECT DISTINCT blob_hash
                FROM representations
                WHERE blob_hash IS NOT NULL
                  AND is_current = 1
            """).fetchall()
            )

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
        textual_payload: str | None = None,
    ) -> DerivedRepresentation:
        """Create a new pending representation.

        Args:
            resource_version_id: Source resource version
            kind: Representation kind
            media_type: MIME type of representation
            producer: Producer information
            pipeline_fingerprint: Pipeline fingerprint
            locators: Evidence locators
            textual_payload: Optional text content for text-based representations

        Returns:
            Created pending representation
        """
        timestamp = _utc_now()

        # For text-based kinds, we need a placeholder textual_payload even if pending
        text_kinds = {
            MediaRepresentationKind.EXTRACTED_TEXT,
            MediaRepresentationKind.OCR_TEXT,
            MediaRepresentationKind.IMAGE_CAPTION,
            MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        }

        if kind in text_kinds and textual_payload is None:
            textual_payload = ""  # Empty placeholder for pending text representations

        representation = DerivedRepresentation(
            id=uuid4(),
            resource_version_id=resource_version_id,
            kind=kind,
            media_type=media_type,
            status=MediaRepresentationStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
            textual_payload=textual_payload,
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

        # Update the representation in place
        timestamp = _utc_now()

        # Update fields that changed
        updated_textual_payload = (
            textual_payload if textual_payload is not None else representation.textual_payload
        )
        updated_blob_reference = (
            blob_reference if blob_reference is not None else representation.blob_reference
        )
        updated_blob_hash = blob_hash if blob_hash is not None else representation.blob_hash
        updated_blob_byte_count = (
            blob_byte_count if blob_byte_count is not None else representation.blob_byte_count
        )
        updated_coverage = coverage or MediaCoverage(is_complete=True, coverage_fraction=1.0)

        # Directly update the database
        with self._registry._database.connection() as conn:
            # If making current, mark existing current representations as non-current
            conn.execute(
                """
                UPDATE representations
                SET is_current = 0, updated_at = ?
                WHERE resource_version_id = ?
                  AND kind = ?
                  AND is_current = 1
            """,
                (
                    timestamp.isoformat(),
                    str(representation.resource_version_id),
                    representation.kind.value,
                ),
            )

            # Update this representation to current
            conn.execute(
                """
                UPDATE representations
                SET status = ?, updated_at = ?, textual_payload = ?,
                    blob_reference = ?, blob_hash = ?, blob_byte_count = ?,
                    coverage_fraction = ?, coverage_is_complete = ?, coverage_detail = ?,
                    confidence = ?, is_current = 1
                WHERE id = ?
            """,
                (
                    MediaRepresentationStatus.CURRENT.value,
                    timestamp.isoformat(),
                    updated_textual_payload,
                    updated_blob_reference,
                    str(updated_blob_hash) if updated_blob_hash else None,
                    updated_blob_byte_count,
                    updated_coverage.coverage_fraction,
                    1 if updated_coverage.is_complete else 0,
                    updated_coverage.detail,
                    confidence,
                    str(representation_id),
                ),
            )

        # Return the updated representation
        return DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.CURRENT,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=updated_textual_payload,
            blob_reference=updated_blob_reference,
            blob_hash=updated_blob_hash,
            blob_byte_count=updated_blob_byte_count,
            locators=representation.locators,
            coverage=updated_coverage,
            confidence=confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
        )

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

        # Update fields that changed
        updated_textual_payload = (
            textual_payload if textual_payload is not None else representation.textual_payload
        )
        updated_blob_reference = (
            blob_reference if blob_reference is not None else representation.blob_reference
        )
        updated_blob_hash = blob_hash if blob_hash is not None else representation.blob_hash
        updated_blob_byte_count = (
            blob_byte_count if blob_byte_count is not None else representation.blob_byte_count
        )

        # Directly update the database
        with self._registry._database.connection() as conn:
            conn.execute(
                """
                UPDATE representations
                SET status = ?, updated_at = ?, textual_payload = ?,
                    blob_reference = ?, blob_hash = ?, blob_byte_count = ?,
                    coverage_fraction = ?, coverage_is_complete = ?, coverage_detail = ?
                WHERE id = ?
            """,
                (
                    MediaRepresentationStatus.PARTIAL.value,
                    timestamp.isoformat(),
                    updated_textual_payload,
                    updated_blob_reference,
                    str(updated_blob_hash) if updated_blob_hash else None,
                    updated_blob_byte_count,
                    coverage.coverage_fraction,
                    1 if coverage.is_complete else 0,
                    coverage.detail,
                    str(representation_id),
                ),
            )

        # Return the updated representation
        return DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.PARTIAL,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=updated_textual_payload,
            blob_reference=updated_blob_reference,
            blob_hash=updated_blob_hash,
            blob_byte_count=updated_blob_byte_count,
            locators=representation.locators,
            coverage=coverage,
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
        )

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

        # Directly update the database
        with self._registry._database.connection() as conn:
            conn.execute(
                """
                UPDATE representations
                SET status = ?, updated_at = ?,
                    coverage_fraction = ?, coverage_is_complete = ?, coverage_detail = ?,
                    error_category = ?, error_message = ?, error_is_retriable = ?, error_diagnostic = ?
                WHERE id = ?
            """,
                (
                    MediaRepresentationStatus.UNAVAILABLE.value,
                    timestamp.isoformat(),
                    0.0,  # coverage_fraction
                    0,  # coverage_is_complete
                    "unavailable",  # coverage_detail
                    error.error_category,
                    error.error_message,
                    1 if error.is_retriable else 0,
                    json.dumps(error.diagnostic_info),
                    str(representation_id),
                ),
            )

        # Return the updated representation
        return DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.UNAVAILABLE,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=representation.textual_payload
            or "",  # Ensure textual_payload for text kinds
            blob_reference=representation.blob_reference,
            blob_hash=representation.blob_hash,
            blob_byte_count=representation.blob_byte_count,
            locators=representation.locators,
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0, detail="unavailable"),
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
            error=error,
        )

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

        # Directly update the database
        with self._registry._database.connection() as conn:
            conn.execute(
                """
                UPDATE representations
                SET status = ?, updated_at = ?,
                    coverage_fraction = ?, coverage_is_complete = ?, coverage_detail = ?,
                    error_category = ?, error_message = ?, error_is_retriable = ?, error_diagnostic = ?
                WHERE id = ?
            """,
                (
                    MediaRepresentationStatus.FAILED.value,
                    timestamp.isoformat(),
                    0.0,  # coverage_fraction
                    0,  # coverage_is_complete
                    "failed",  # coverage_detail
                    error.error_category,
                    error.error_message,
                    1 if error.is_retriable else 0,
                    json.dumps(error.diagnostic_info),
                    str(representation_id),
                ),
            )

        # Return the updated representation
        return DerivedRepresentation(
            id=representation.id,
            resource_version_id=representation.resource_version_id,
            kind=representation.kind,
            media_type=representation.media_type,
            status=MediaRepresentationStatus.FAILED,
            created_at=representation.created_at,
            updated_at=timestamp,
            textual_payload=representation.textual_payload
            or "",  # Ensure textual_payload for text kinds
            blob_reference=representation.blob_reference,
            blob_hash=representation.blob_hash,
            blob_byte_count=representation.blob_byte_count,
            locators=representation.locators,
            coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0, detail="failed"),
            confidence=representation.confidence,
            producer=representation.producer,
            pipeline_fingerprint=representation.pipeline_fingerprint,
            error=error,
        )
