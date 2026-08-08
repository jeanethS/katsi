"""SQLite repository primitives for authoritative workspace state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import (
    require_resource_versions,
    require_workspace_version,
    write_transaction,
)
from katsi_core.workspace.contracts import (
    ChangeSetId,
    Resource,
    ResourceId,
    ResourceStatus,
    ResourceVersion,
    Workspace,
    WorkspaceEvent,
    WorkspaceEventKind,
    WorkspaceId,
    WorkspaceStatus,
)
from katsi_core.workspace.errors import ConflictError


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkspaceRepository:
    """Authoritative current-state and append-only event repository."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def register_workspace(self, root: Path, display_name: str) -> Workspace:
        """Register a canonical root with a stable identity and first event."""
        canonical_root = root.resolve(strict=True)
        if not canonical_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {canonical_root}")
        timestamp = _utc_now()
        workspace_id = uuid4()
        with self._database.connection() as connection, write_transaction(connection):
            self._require_non_overlapping_root(connection, canonical_root)
            connection.execute(
                "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(workspace_id),
                    str(canonical_root),
                    display_name,
                    "active",
                    1,
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO workspace_roots VALUES (?, ?, ?, ?, ?)",
                (str(workspace_id), str(canonical_root), 1, timestamp.isoformat(), None),
            )
            self._insert_event(
                connection,
                WorkspaceEvent(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    sequence=1,
                    kind=WorkspaceEventKind.WORKSPACE_REGISTERED,
                    occurred_at=timestamp,
                    detail={"root_path": str(canonical_root)},
                ),
            )
        workspace = self.get_workspace(workspace_id)
        assert workspace is not None
        return workspace

    def relocate_workspace(
        self, workspace_id: WorkspaceId, expected_state_version: int, new_root: Path
    ) -> Workspace:
        """Move a workspace root without changing its stable workspace identity."""
        canonical_root = new_root.resolve(strict=True)
        if not canonical_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {canonical_root}")
        timestamp = _utc_now()
        with self._database.connection() as connection, write_transaction(connection):
            require_workspace_version(connection, workspace_id, expected_state_version)
            self._require_non_overlapping_root(connection, canonical_root, excluding=workspace_id)
            old_root = connection.execute(
                "SELECT root_path FROM workspaces WHERE id = ?", (str(workspace_id),)
            ).fetchone()[0]
            sequence = self._next_event_sequence(connection, workspace_id)
            connection.execute(
                "UPDATE workspaces SET root_path = ?, state_version = state_version + 1, updated_at = ? WHERE id = ?",
                (str(canonical_root), timestamp.isoformat(), str(workspace_id)),
            )
            connection.execute(
                "UPDATE workspace_roots SET active = 0, retired_at = ? WHERE workspace_id = ? AND active = 1",
                (timestamp.isoformat(), str(workspace_id)),
            )
            connection.execute(
                "INSERT INTO workspace_roots VALUES (?, ?, ?, ?, ?)",
                (str(workspace_id), str(canonical_root), 1, timestamp.isoformat(), None),
            )
            self._insert_event(
                connection,
                WorkspaceEvent(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    sequence=sequence,
                    kind=WorkspaceEventKind.WORKSPACE_RELOCATED,
                    occurred_at=timestamp,
                    detail={"from_root": old_root, "to_root": str(canonical_root)},
                ),
            )
        workspace = self.get_workspace(workspace_id)
        assert workspace is not None
        return workspace

    def create_resource(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        path: str,
        content_hash: str,
        byte_count: int,
        *,
        event_kind: WorkspaceEventKind = WorkspaceEventKind.RESOURCE_CREATED,
        correlation_id: ChangeSetId | None = None,
    ) -> ResourceVersion:
        """Create a stable logical resource and its first immutable content version."""
        resource_id = uuid4()
        return self._record_resource_version(
            workspace_id,
            expected_workspace_version,
            resource_id,
            expected_resource_version=None,
            path=path,
            content_hash=content_hash,
            byte_count=byte_count,
            status=ResourceStatus.CURRENT,
            kind=event_kind,
            correlation_id=correlation_id,
        )

    def update_resource(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        expected_resource_version: int,
        content_hash: str,
        byte_count: int,
        *,
        event_kind: WorkspaceEventKind = WorkspaceEventKind.RESOURCE_UPDATED,
        correlation_id: ChangeSetId | None = None,
    ) -> ResourceVersion:
        """Record an immutable content version while retaining logical resource identity."""
        resource = self.get_resource(resource_id)
        if resource is None or resource.current_path is None:
            raise ConflictError(f"resource {resource_id} is not current")
        return self._record_resource_version(
            workspace_id,
            expected_workspace_version,
            resource_id,
            expected_resource_version=expected_resource_version,
            path=resource.current_path,
            content_hash=content_hash,
            byte_count=byte_count,
            status=ResourceStatus.CURRENT,
            kind=event_kind,
            correlation_id=correlation_id,
        )

    def move_resource(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        expected_resource_version: int,
        destination_path: str,
        *,
        event_kind: WorkspaceEventKind = WorkspaceEventKind.RESOURCE_MOVED,
        correlation_id: ChangeSetId | None = None,
    ) -> WorkspaceEvent:
        """Move a resource without changing its stable identity or content history."""
        return self._transition_resource(
            workspace_id,
            expected_workspace_version,
            resource_id,
            expected_resource_version,
            path=destination_path,
            status=ResourceStatus.CURRENT,
            kind=event_kind,
            correlation_id=correlation_id,
        )

    def delete_resource(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        expected_resource_version: int,
        *,
        event_kind: WorkspaceEventKind = WorkspaceEventKind.RESOURCE_DELETED,
        correlation_id: ChangeSetId | None = None,
    ) -> WorkspaceEvent:
        """Preserve historical resource evidence while removing it from current state."""
        return self._transition_resource(
            workspace_id,
            expected_workspace_version,
            resource_id,
            expected_resource_version,
            path=None,
            status=ResourceStatus.DELETED,
            kind=event_kind,
            correlation_id=correlation_id,
        )

    def mark_resource_ambiguous(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        expected_resource_version: int,
    ) -> WorkspaceEvent:
        """Record a move inference ambiguity instead of merging candidate identities."""
        resource = self.get_resource(resource_id)
        return self._transition_resource(
            workspace_id,
            expected_workspace_version,
            resource_id,
            expected_resource_version,
            path=resource.current_path if resource else None,
            status=ResourceStatus.AMBIGUOUS,
            kind=WorkspaceEventKind.RESOURCE_AMBIGUOUS,
            correlation_id=None,
        )

    def _record_resource_version(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        *,
        expected_resource_version: int | None,
        path: str,
        content_hash: str,
        byte_count: int,
        status: ResourceStatus,
        kind: WorkspaceEventKind,
        correlation_id: ChangeSetId | None,
    ) -> ResourceVersion:
        timestamp = _utc_now()
        with self._database.connection() as connection, write_transaction(connection):
            require_workspace_version(connection, workspace_id, expected_workspace_version)
            sequence = self._next_event_sequence(connection, workspace_id)
            event = WorkspaceEvent(
                id=uuid4(),
                workspace_id=workspace_id,
                sequence=sequence,
                kind=kind,
                occurred_at=timestamp,
                resource_id=resource_id,
                correlation_id=correlation_id,
                detail={"path": path, "content_hash": content_hash},
            )
            if expected_resource_version is None:
                connection.execute(
                    "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(resource_id),
                        str(workspace_id),
                        path,
                        status.value,
                        1,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                    ),
                )
            else:
                require_resource_versions(connection, {resource_id: expected_resource_version})
                connection.execute(
                    """
                    UPDATE resources SET state_version = state_version + 1, status = ?, updated_at = ?
                    WHERE id = ? AND workspace_id = ?
                    """,
                    (status.value, timestamp.isoformat(), str(resource_id), str(workspace_id)),
                )
            connection.execute(
                "UPDATE workspaces SET state_version = state_version + 1, updated_at = ? WHERE id = ?",
                (timestamp.isoformat(), str(workspace_id)),
            )
            self._insert_event(connection, event)
            existing = connection.execute(
                """
                SELECT id, byte_count, observed_at, source_event_id FROM resource_versions
                WHERE resource_id = ? AND content_hash = ?
                """,
                (str(resource_id), content_hash),
            ).fetchone()
            version_id = UUID(existing["id"]) if existing else uuid4()
            if existing is None:
                connection.execute(
                    "INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(version_id),
                        str(resource_id),
                        content_hash,
                        byte_count,
                        timestamp.isoformat(),
                        str(event.id),
                    ),
                )
        if existing is not None:
            return ResourceVersion(
                id=version_id,
                resource_id=resource_id,
                content_hash=content_hash,
                byte_count=existing["byte_count"],
                observed_at=datetime.fromisoformat(existing["observed_at"]),
                source_event_id=UUID(existing["source_event_id"]),
            )
        return ResourceVersion(
            id=version_id,
            resource_id=resource_id,
            content_hash=content_hash,
            byte_count=byte_count,
            observed_at=timestamp,
            source_event_id=event.id,
        )

    def _transition_resource(
        self,
        workspace_id: WorkspaceId,
        expected_workspace_version: int,
        resource_id: ResourceId,
        expected_resource_version: int,
        *,
        path: str | None,
        status: ResourceStatus,
        kind: WorkspaceEventKind,
        correlation_id: ChangeSetId | None,
    ) -> WorkspaceEvent:
        timestamp = _utc_now()
        with self._database.connection() as connection, write_transaction(connection):
            require_workspace_version(connection, workspace_id, expected_workspace_version)
            require_resource_versions(connection, {resource_id: expected_resource_version})
            sequence = self._next_event_sequence(connection, workspace_id)
            event = WorkspaceEvent(
                id=uuid4(),
                workspace_id=workspace_id,
                sequence=sequence,
                kind=kind,
                occurred_at=timestamp,
                resource_id=resource_id,
                correlation_id=correlation_id,
                detail={"path": path or ""},
            )
            connection.execute(
                """
                UPDATE resources SET current_path = ?, status = ?, state_version = state_version + 1,
                    updated_at = ? WHERE id = ? AND workspace_id = ?
                """,
                (path, status.value, timestamp.isoformat(), str(resource_id), str(workspace_id)),
            )
            connection.execute(
                "UPDATE workspaces SET state_version = state_version + 1, updated_at = ? WHERE id = ?",
                (timestamp.isoformat(), str(workspace_id)),
            )
            self._insert_event(connection, event)
        return event

    def append_event(
        self,
        workspace_id: WorkspaceId,
        expected_state_version: int,
        kind: WorkspaceEventKind,
        *,
        resource_id: ResourceId | None = None,
        correlation_id: ChangeSetId | None = None,
        detail: Mapping[str, str] | None = None,
        projection_payloads: Mapping[str, Mapping[str, str]] | None = None,
    ) -> WorkspaceEvent:
        """Append a sequenced event and update current workspace state atomically."""
        timestamp = _utc_now()
        event_id = uuid4()
        with self._database.connection() as connection, write_transaction(connection):
            require_workspace_version(connection, workspace_id, expected_state_version)
            sequence = self._next_event_sequence(connection, workspace_id)
            connection.execute(
                "UPDATE workspaces SET state_version = state_version + 1, updated_at = ? WHERE id = ?",
                (timestamp.isoformat(), str(workspace_id)),
            )
            event = WorkspaceEvent(
                id=event_id,
                workspace_id=workspace_id,
                sequence=sequence,
                kind=kind,
                occurred_at=timestamp,
                resource_id=resource_id,
                correlation_id=correlation_id,
                detail=dict(detail or {}),
            )
            self._insert_event(connection, event)
            for projection_name, payload in (projection_payloads or {}).items():
                connection.execute(
                    """
                    INSERT INTO projection_outbox
                        (workspace_id, event_id, projection_name, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(workspace_id),
                        str(event.id),
                        projection_name,
                        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")),
                        timestamp.isoformat(),
                    ),
                )
        return event

    @staticmethod
    def _next_event_sequence(connection: object, workspace_id: WorkspaceId) -> int:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM workspace_events WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _insert_event(connection: object, event: WorkspaceEvent) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO workspace_events
                (id, workspace_id, sequence, kind, occurred_at, resource_id, correlation_id, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.id),
                str(event.workspace_id),
                event.sequence,
                event.kind.value,
                event.occurred_at.isoformat(),
                str(event.resource_id) if event.resource_id else None,
                str(event.correlation_id) if event.correlation_id else None,
                json.dumps(event.detail, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _require_non_overlapping_root(
        connection: object, root: Path, *, excluding: WorkspaceId | None = None
    ) -> None:
        query = "SELECT workspace_id, root_path FROM workspace_roots WHERE active = 1"
        params: tuple[str, ...] = ()
        if excluding is not None:
            query += " AND workspace_id != ?"
            params = (str(excluding),)
        rows = connection.execute(query, params).fetchall()  # type: ignore[attr-defined]
        for row in rows:
            existing = Path(row["root_path"])
            if root.is_relative_to(existing) or existing.is_relative_to(root):
                raise ConflictError(
                    f"workspace root overlaps registered workspace {row['workspace_id']}"
                )

    def get_workspace(self, workspace_id: WorkspaceId) -> Workspace | None:
        """Read current authoritative workspace state."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (str(workspace_id),)
            ).fetchone()
        if row is None:
            return None
        return Workspace(
            id=UUID(row["id"]),
            root_path=row["root_path"],
            display_name=row["display_name"],
            status=WorkspaceStatus(row["status"]),
            state_version=row["state_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_resource(self, resource_id: ResourceId) -> Resource | None:
        """Read the current logical resource state without conflating it with content."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM resources WHERE id = ?", (str(resource_id),)
            ).fetchone()
        if row is None:
            return None
        return Resource(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            current_path=row["current_path"],
            status=ResourceStatus(row["status"]),
            state_version=row["state_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list_current_resources(self, workspace_id: WorkspaceId) -> list[Resource]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM resources WHERE workspace_id = ? AND status = 'current'",
                (str(workspace_id),),
            ).fetchall()
        return [
            Resource(
                id=UUID(row["id"]),
                workspace_id=UUID(row["workspace_id"]),
                current_path=row["current_path"],
                status=ResourceStatus(row["status"]),
                state_version=row["state_version"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def current_content_hash(self, resource_id: ResourceId) -> str | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT content_hash FROM resource_versions WHERE resource_id = ? ORDER BY observed_at DESC LIMIT 1",
                (str(resource_id),),
            ).fetchone()
        return row["content_hash"] if row else None

    def list_events(
        self, workspace_id: WorkspaceId, *, after_sequence: int = 0, limit: int = 100
    ) -> list[WorkspaceEvent]:
        """Return ordered append-only history after a caller's cursor."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_events
                WHERE workspace_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (str(workspace_id), after_sequence, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def last_event_sequence(self, workspace_id: WorkspaceId) -> int:
        """Return the highest sequenced event for a workspace (0 when none)."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS seq FROM workspace_events WHERE workspace_id = ?",
                (str(workspace_id),),
            ).fetchone()
        return int(row["seq"])

    def recent_events(self, workspace_id: WorkspaceId, *, limit: int) -> list[WorkspaceEvent]:
        """Return the most recent workspace events, newest first."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_events
                WHERE workspace_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (str(workspace_id), limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: object) -> WorkspaceEvent:
        return WorkspaceEvent(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            sequence=row["sequence"],
            kind=WorkspaceEventKind(row["kind"]),
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            resource_id=UUID(row["resource_id"]) if row["resource_id"] else None,
            correlation_id=UUID(row["correlation_id"]) if row["correlation_id"] else None,
            detail=json.loads(row["detail_json"]),
        )
