"""Transactional-outbox delivery for rebuildable projections."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from katsi_core.config import ProjectionWorkerSettings
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import WorkspaceId


@dataclass(frozen=True, slots=True)
class ProjectionOutboxEntry:
    id: int
    workspace_id: WorkspaceId
    event_id: str
    payload: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProjectionOffset:
    projection_name: str
    workspace_id: WorkspaceId
    outbox_id: int


ProjectionHandler = Callable[[ProjectionOutboxEntry], None]


class ProjectionWorker:
    """Deliver outbox rows in order; handlers must tolerate repeat delivery."""

    def __init__(self, database: WorkspaceSQLite, settings: ProjectionWorkerSettings) -> None:
        self._database = database
        self._settings = settings

    def run(
        self,
        workspace_id: WorkspaceId,
        projection_name: str,
        handler: ProjectionHandler,
    ) -> int:
        """Deliver at most one configured batch and durably advance its offset.

        Delivery intentionally happens outside the SQLite transaction.  A process
        failure between delivery and offset advancement results in a replay, not
        a lost authoritative event.
        """
        delivered = 0
        for entry in self._entries_after_offset(workspace_id, projection_name):
            handler(entry)
            self._advance_offset(projection_name, workspace_id, entry.id)
            delivered += 1
        return delivered

    def offset(self, workspace_id: WorkspaceId, projection_name: str) -> ProjectionOffset:
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT outbox_id FROM projection_offsets
                WHERE workspace_id = ? AND projection_name = ?""",
                (str(workspace_id), projection_name),
            ).fetchone()
        return ProjectionOffset(
            projection_name=projection_name,
            workspace_id=workspace_id,
            outbox_id=int(row["outbox_id"]) if row else 0,
        )

    def _entries_after_offset(
        self, workspace_id: WorkspaceId, projection_name: str
    ) -> list[ProjectionOutboxEntry]:
        offset = self.offset(workspace_id, projection_name).outbox_id
        with self._database.connection() as connection:
            rows = connection.execute(
                """SELECT id, workspace_id, event_id, payload_json FROM projection_outbox
                WHERE workspace_id = ? AND projection_name = ? AND id > ?
                ORDER BY id ASC LIMIT ?""",
                (str(workspace_id), projection_name, offset, self._settings.batch_size),
            ).fetchall()
        return [
            ProjectionOutboxEntry(
                id=int(row["id"]),
                workspace_id=workspace_id,
                event_id=row["event_id"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def _advance_offset(
        self, projection_name: str, workspace_id: WorkspaceId, outbox_id: int
    ) -> None:
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                """INSERT INTO projection_offsets (projection_name, workspace_id, outbox_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(projection_name, workspace_id) DO UPDATE SET
                    outbox_id = MAX(projection_offsets.outbox_id, excluded.outbox_id),
                    updated_at = excluded.updated_at""",
                (projection_name, str(workspace_id), outbox_id, datetime.now(UTC).isoformat()),
            )
