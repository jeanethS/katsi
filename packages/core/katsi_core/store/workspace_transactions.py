"""Short SQLite transaction helpers with optimistic state checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from uuid import UUID

from katsi_core.workspace.errors import StaleStateError


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a short immediate transaction, rolling back every failed command."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def require_workspace_version(
    connection: sqlite3.Connection, workspace_id: UUID, expected_state_version: int
) -> None:
    """Raise a typed stale-state error when the workspace changed concurrently."""
    row = connection.execute(
        "SELECT state_version FROM workspaces WHERE id = ?", (str(workspace_id),)
    ).fetchone()
    if row is None:
        raise StaleStateError(f"workspace {workspace_id} does not exist")
    actual_version = int(row[0])
    if actual_version != expected_state_version:
        raise StaleStateError(
            f"workspace {workspace_id} expected version {expected_state_version}, found {actual_version}"
        )


def require_resource_versions(
    connection: sqlite3.Connection, expected_versions: Mapping[UUID, int]
) -> None:
    """Require exact current versions for all resources touched by a command."""
    for resource_id, expected_version in expected_versions.items():
        row = connection.execute(
            "SELECT state_version FROM resources WHERE id = ?", (str(resource_id),)
        ).fetchone()
        if row is None:
            raise StaleStateError(f"resource {resource_id} does not exist")
        actual_version = int(row[0])
        if actual_version != expected_version:
            raise StaleStateError(
                f"resource {resource_id} expected version {expected_version}, found {actual_version}"
            )
