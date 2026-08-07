"""Tests for the configured authoritative SQLite connection factory."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store import (
    WorkspaceRepository,
    WorkspaceSQLite,
    apply_migrations,
    require_resource_versions,
    require_workspace_version,
    write_transaction,
)
from katsi_core.workspace.contracts import WorkspaceEventKind
from katsi_core.workspace.errors import ConflictError, StaleStateError, UnsupportedOperationError


def test_connection_factory_configures_sqlite_and_cleans_up(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(
        tmp_path / "state" / "workspace.sqlite3", SQLiteSettings(busy_timeout_ms=123)
    )

    connection = factory.connect()

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 123
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    factory.close_all()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_connection_context_closes_connection(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())

    with factory.connection() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_factory_opens_independent_connections(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())

    first = factory.connect()
    second = factory.connect()

    assert first is not second
    factory.close_all()


def test_factory_refuses_a_newer_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "workspace.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 2")
    connection.close()
    factory = WorkspaceSQLite(database_path, SQLiteSettings(schema_version=1))

    with pytest.raises(UnsupportedOperationError, match="newer than supported"):
        factory.connect()


def test_initial_migration_creates_every_authoritative_table_idempotently(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    expected_tables = {
        "workspaces",
        "workspace_roots",
        "resources",
        "resource_versions",
        "workspace_events",
        "content_enrichments",
        "agent_identities",
        "agent_credentials",
        "capability_grants",
        "claims",
        "claim_evidence",
        "claim_transitions",
        "open_work",
        "work_leases",
        "change_sets",
        "change_set_dependencies",
        "change_set_operations",
        "change_set_transitions",
        "action_journal",
        "recovery_blobs",
        "projection_outbox",
        "projection_offsets",
    }

    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
        apply_migrations(connection, target_version=1)
        actual_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert expected_tables <= actual_tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_transaction_helpers_detect_stale_state_and_roll_back(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    workspace_id = "f0a63ec8-2b42-48ce-85d9-edb6cc4d4fef"
    resource_id = "a4a4032b-6d32-4d0e-b3a5-aa5f3a913d5f"
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
        connection.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, "/project", "Project", "active", 2, "now", "now"),
        )
        connection.execute(
            "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)",
            (resource_id, workspace_id, "readme.md", "current", 3, "now", "now"),
        )
        connection.execute(
            "INSERT INTO agent_identities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, "Owner", "test", None, None, 1, "now", None),
        )

        with pytest.raises(StaleStateError, match="expected version 1, found 2"):
            require_workspace_version(connection, UUID(workspace_id), expected_state_version=1)
        with pytest.raises(StaleStateError, match="expected version 4, found 3"):
            require_resource_versions(connection, {UUID(resource_id): 4})

        with pytest.raises(RuntimeError), write_transaction(connection):
            connection.execute(
                "INSERT INTO open_work VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("work", workspace_id, workspace_id, "x", "open", "now", "now"),
            )
            raise RuntimeError("force rollback")
        assert connection.execute("SELECT COUNT(*) FROM open_work").fetchone()[0] == 0


def test_repository_appends_events_updates_current_state_and_outbox_atomically(
    tmp_path: Path,
) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    workspace_id = "f0a63ec8-2b42-48ce-85d9-edb6cc4d4fef"
    timestamp = datetime.now(UTC).isoformat()
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
        connection.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, "/project", "Project", "active", 0, timestamp, timestamp),
        )
    repository = WorkspaceRepository(factory)

    event = repository.append_event(
        UUID(workspace_id),
        expected_state_version=0,
        kind=WorkspaceEventKind.RESOURCE_CREATED,
        detail={"path": "readme.md"},
        projection_payloads={"graph": {"action": "upsert"}, "vector": {"action": "upsert"}},
    )

    assert event.sequence == 1
    workspace = repository.get_workspace(UUID(workspace_id))
    assert workspace is not None
    assert workspace.state_version == 1
    assert [entry.sequence for entry in repository.list_events(UUID(workspace_id))] == [1]
    with factory.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM projection_outbox").fetchone()[0] == 2
    with pytest.raises(StaleStateError):
        repository.append_event(
            UUID(workspace_id),
            expected_state_version=0,
            kind=WorkspaceEventKind.RESOURCE_UPDATED,
        )
    assert [entry.sequence for entry in repository.list_events(UUID(workspace_id))] == [1]


def test_repository_event_history_is_ordered_and_pageable(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    workspace_id = "f0a63ec8-2b42-48ce-85d9-edb6cc4d4fef"
    timestamp = datetime.now(UTC).isoformat()
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
        connection.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, "/project", "Project", "active", 0, timestamp, timestamp),
        )
    repository = WorkspaceRepository(factory)
    for version in range(3):
        repository.append_event(
            UUID(workspace_id),
            expected_state_version=version,
            kind=WorkspaceEventKind.RESOURCE_UPDATED,
        )

    assert [event.sequence for event in repository.list_events(UUID(workspace_id), limit=2)] == [
        1,
        2,
    ]
    assert [
        event.sequence for event in repository.list_events(UUID(workspace_id), after_sequence=2)
    ] == [3]


def test_separate_connections_prove_conflict_uniqueness_and_timeout(tmp_path: Path) -> None:
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings(busy_timeout_ms=1_000))
    workspace_id = UUID("f0a63ec8-2b42-48ce-85d9-edb6cc4d4fef")
    timestamp = datetime.now(UTC).isoformat()
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
        connection.execute(
            "INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(workspace_id), "/project", "Project", "active", 0, timestamp, timestamp),
        )
    repository = WorkspaceRepository(factory)

    def append_once() -> str:
        try:
            return str(
                repository.append_event(
                    workspace_id,
                    expected_state_version=0,
                    kind=WorkspaceEventKind.RESOURCE_CREATED,
                ).sequence
            )
        except StaleStateError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _unused: append_once(), range(2)))
    assert sorted(outcomes) == ["1", "stale"]
    assert [event.sequence for event in repository.list_events(workspace_id)] == [1]

    timeout_factory = WorkspaceSQLite(factory.database_path, SQLiteSettings(busy_timeout_ms=0))
    blocker = factory.connect()
    blocker.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        WorkspaceRepository(timeout_factory).append_event(
            workspace_id,
            expected_state_version=1,
            kind=WorkspaceEventKind.RESOURCE_UPDATED,
        )
    blocker.execute("ROLLBACK")
    timeout_factory.close_all()
    factory.close_all()


def test_workspace_registration_rejects_overlapping_canonical_roots_and_relocation_preserves_id(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    nested_root = first_root / "nested"
    second_root = tmp_path / "second"
    first_root.mkdir()
    nested_root.mkdir()
    second_root.mkdir()
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)

    workspace = repository.register_workspace(first_root, "First")

    with pytest.raises(ConflictError, match="overlaps"):
        repository.register_workspace(nested_root, "Nested")
    relocated = repository.relocate_workspace(workspace.id, workspace.state_version, second_root)

    assert relocated.id == workspace.id
    assert relocated.root_path == str(second_root.resolve())
    assert relocated.state_version == workspace.state_version + 1
    assert [event.kind for event in repository.list_events(workspace.id)] == [
        WorkspaceEventKind.WORKSPACE_REGISTERED,
        WorkspaceEventKind.WORKSPACE_RELOCATED,
    ]
