"""Authoritative active workspace intent."""

from __future__ import annotations

from datetime import UTC, datetime

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import WorkspaceId
from katsi_core.workspace.errors import ConflictError


class IntentService:
    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def activate(
        self, workspace_id: WorkspaceId, goal: str, expected_version: int | None = None
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT version FROM workspace_intents WHERE workspace_id = ?", (str(workspace_id),)
            ).fetchone()
            current = int(row["version"]) if row else 0
            if expected_version is not None and expected_version != current:
                raise ConflictError(
                    f"intent version conflict: expected {expected_version}, found {current}"
                )
            version = current + 1
            connection.execute(
                "INSERT INTO workspace_intents VALUES (?, ?, ?, ?) "
                "ON CONFLICT(workspace_id) DO UPDATE SET goal = excluded.goal, version = excluded.version, updated_at = excluded.updated_at",
                (str(workspace_id), goal, version, now),
            )
        return version

    def get(self, workspace_id: WorkspaceId) -> tuple[str, int] | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT goal, version FROM workspace_intents WHERE workspace_id = ?",
                (str(workspace_id),),
            ).fetchone()
        return (row["goal"], int(row["version"])) if row else None
