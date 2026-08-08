"""Tests for the configured authoritative SQLite connection factory."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from katsi_core.config import IngestSettings, ObserverSettings, SQLiteSettings
from katsi_core.store import (
    LegacyCleanupGuard,
    LegacyFileRecordImporter,
    WorkspaceRepository,
    WorkspaceSQLite,
    apply_migrations,
    require_resource_versions,
    require_workspace_version,
    write_transaction,
)
from katsi_core.workspace.contracts import WorkspaceEventKind
from katsi_core.workspace.errors import ConflictError, StaleStateError, UnsupportedOperationError
from katsi_core.workspace.observer import FilesystemEvent, FilesystemEventKind
from katsi_core.workspace.reconcile import WorkspaceReconciler


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


def test_resource_versions_keep_logical_identity_through_content_and_path_lifecycle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    hash_a = "a" * 64
    hash_b = "b" * 64

    first = repository.create_resource(workspace.id, workspace.state_version, "one.md", hash_a, 10)
    resource = repository.get_resource(first.resource_id)
    assert resource is not None
    second = repository.update_resource(
        workspace.id,
        workspace.state_version + 1,
        first.resource_id,
        resource.state_version,
        hash_b,
        20,
    )
    resource = repository.get_resource(first.resource_id)
    assert resource is not None
    move = repository.move_resource(
        workspace.id,
        workspace.state_version + 2,
        first.resource_id,
        resource.state_version,
        "moved.md",
    )
    moved = repository.get_resource(first.resource_id)
    assert moved is not None
    ambiguity = repository.mark_resource_ambiguous(
        workspace.id, workspace.state_version + 3, first.resource_id, moved.state_version
    )
    ambiguous = repository.get_resource(first.resource_id)
    assert ambiguous is not None
    deleted = repository.delete_resource(
        workspace.id, workspace.state_version + 4, first.resource_id, ambiguous.state_version
    )

    assert first.resource_id == second.resource_id
    assert first.id != second.id
    assert move.kind is WorkspaceEventKind.RESOURCE_MOVED
    assert ambiguity.kind is WorkspaceEventKind.RESOURCE_AMBIGUOUS
    assert deleted.kind is WorkspaceEventKind.RESOURCE_DELETED
    final = repository.get_resource(first.resource_id)
    assert final is not None
    assert final.current_path is None
    assert final.status.value == "deleted"


def test_duplicate_content_does_not_merge_resources_and_returning_hash_reuses_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    content_hash = "c" * 64
    first = repository.create_resource(workspace.id, 1, "one.md", content_hash, 10)
    second = repository.create_resource(workspace.id, 2, "two.md", content_hash, 10)
    first_current = repository.get_resource(first.resource_id)
    assert first_current is not None
    changed = repository.update_resource(
        workspace.id, 3, first.resource_id, first_current.state_version, "d" * 64, 11
    )
    changed_current = repository.get_resource(first.resource_id)
    assert changed_current is not None
    returned = repository.update_resource(
        workspace.id, 4, first.resource_id, changed_current.state_version, content_hash, 10
    )

    assert first.resource_id != second.resource_id
    assert first.id == returned.id
    assert first.id != changed.id


def test_legacy_import_is_read_only_idempotent_and_preserves_summary(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    current = root / "current.md"
    current.write_text("current", encoding="utf-8")
    missing = root / "missing.md"
    legacy_path = tmp_path / "file_records.json"
    records = {
        "current": {
            "id": "current",
            "path": str(current),
            "name": "current.md",
            "ext": ".md",
            "mime": "text/markdown",
            "size_bytes": 7,
            "mtime": 0.0,
            "content_hash": "e" * 64,
            "summary": "Current summary",
        },
        "missing": {
            "id": "missing",
            "path": str(missing),
            "name": "missing.md",
            "ext": ".md",
            "mime": "text/markdown",
            "size_bytes": 7,
            "mtime": 0.0,
            "content_hash": "f" * 64,
        },
        "invalid": {"id": "invalid"},
    }
    legacy_path.write_text(json.dumps(records), encoding="utf-8")
    original_bytes = legacy_path.read_bytes()
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    importer = LegacyFileRecordImporter(factory, repository)

    assert importer.import_file(legacy_path, workspace.id, "legacy-v1") == 2
    assert importer.import_file(legacy_path, workspace.id, "legacy-v1") == 0
    assert legacy_path.read_bytes() == original_bytes
    resources = [
        repository.get_resource(event.resource_id) for event in repository.list_events(workspace.id)
    ]
    assert any(
        resource is not None and resource.status.value == "deleted" for resource in resources
    )
    with factory.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM content_enrichments").fetchone()[0] == 1


def test_legacy_cleanup_requires_reconciliation_and_projection_validation() -> None:
    with pytest.raises(ValueError, match="reconciliation"):
        LegacyCleanupGuard.require_safe_cleanup(
            reconciliation_passed=True, projections_validated=False
        )
    LegacyCleanupGuard.require_safe_cleanup(reconciliation_passed=True, projections_validated=True)


def test_full_scan_converges_create_modify_and_deleted_resources(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    tracked_path = root / "tracked.md"
    tracked_path.write_text("one", encoding="utf-8")
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    reconciler = WorkspaceReconciler(
        repository, IngestSettings(include_globs=["**/*.md"], exclude_globs=[]), ObserverSettings()
    )

    reconciler.full_scan(workspace.id)
    resource = repository.list_current_resources(workspace.id)[0]
    first_hash = repository.current_content_hash(resource.id)
    tracked_path.write_text("two", encoding="utf-8")
    reconciler.full_scan(workspace.id)
    assert repository.current_content_hash(resource.id) != first_hash
    tracked_path.unlink()
    reconciler.full_scan(workspace.id)

    assert repository.list_current_resources(workspace.id) == []
    deleted = repository.get_resource(resource.id)
    assert deleted is not None and deleted.status.value == "deleted"


def test_observer_hints_are_idempotent_and_preserve_explicit_move_identity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    original = root / "original.md"
    original.write_text("one", encoding="utf-8")
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    reconciler = WorkspaceReconciler(
        repository,
        IngestSettings(include_globs=["**/*.md"], exclude_globs=[]),
        ObserverSettings(debounce_seconds=0, stable_read_retry_seconds=0),
    )

    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.CREATED, original, source_sequence=1)
    )
    resource = repository.list_current_resources(workspace.id)[0]
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, original, source_sequence=2)
    )
    assert repository.list_current_resources(workspace.id) == [resource]

    renamed = root / "renamed.md"
    original.rename(renamed)
    reconciler.handle_event(
        workspace.id,
        FilesystemEvent(
            FilesystemEventKind.MOVED,
            original,
            destination_path=renamed,
            source_sequence=3,
        ),
    )
    moved = repository.list_current_resources(workspace.id)
    assert len(moved) == 1
    assert moved[0].id == resource.id
    assert moved[0].current_path == "renamed.md"

    renamed.unlink()
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.DELETED, renamed, source_sequence=4)
    )
    assert repository.list_current_resources(workspace.id) == []


def test_observer_overflow_and_gap_trigger_full_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    path = root / "note.md"
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    reconciler = WorkspaceReconciler(
        repository,
        IngestSettings(include_globs=["**/*.md"], exclude_globs=[]),
        ObserverSettings(debounce_seconds=0, stable_read_retry_seconds=0),
    )

    path.write_text("first", encoding="utf-8")
    reconciler.on_startup(workspace.id)
    resource = repository.list_current_resources(workspace.id)[0]
    path.write_text("second", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=1)
    )
    before_gap = repository.current_content_hash(resource.id)
    path.write_text("third", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=3)
    )
    assert repository.current_content_hash(resource.id) != before_gap

    path.unlink()
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.OVERFLOW, root, source_sequence=4)
    )
    assert repository.list_current_resources(workspace.id) == []


def test_reconciler_classifies_direct_writes_and_correlates_governed_writes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    path = root / "note.md"
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    reconciler = WorkspaceReconciler(
        repository,
        IngestSettings(include_globs=["**/*.md"], exclude_globs=[]),
        ObserverSettings(debounce_seconds=0, stable_read_retry_seconds=0),
    )

    path.write_text("outside", encoding="utf-8")
    reconciler.handle_event(workspace.id, FilesystemEvent(FilesystemEventKind.CREATED, path))
    external_event = repository.list_events(workspace.id)[-1]
    assert external_event.kind is WorkspaceEventKind.EXTERNAL_CHANGE
    assert external_event.correlation_id is None

    correlation_id = uuid4()
    path.write_text("governed", encoding="utf-8")
    reconciler.handle_event(
        workspace.id,
        FilesystemEvent(
            FilesystemEventKind.MODIFIED,
            path,
            correlation_id=correlation_id,
        ),
    )
    governed_event = repository.list_events(workspace.id)[-1]
    assert governed_event.kind is WorkspaceEventKind.RESOURCE_UPDATED
    assert governed_event.correlation_id == correlation_id


def test_reconciler_converges_duplicate_reordered_coalesced_and_missing_events(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    path = root / "note.md"
    factory = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with factory.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(factory)
    workspace = repository.register_workspace(root, "Project")
    reconciler = WorkspaceReconciler(
        repository,
        IngestSettings(include_globs=["**/*.md"], exclude_globs=[]),
        ObserverSettings(debounce_seconds=0, stable_read_retry_seconds=0),
    )

    path.write_text("one", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.CREATED, path, source_sequence=1)
    )
    resource = repository.list_current_resources(workspace.id)[0]

    path.write_text("two", encoding="utf-8")
    duplicate = FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=2)
    reconciler.handle_event(workspace.id, duplicate)
    event_count = len(repository.list_events(workspace.id))
    reconciler.handle_event(workspace.id, duplicate)
    assert len(repository.list_events(workspace.id)) == event_count

    path.write_text("three", encoding="utf-8")
    path.write_text("four", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=3)
    )
    coalesced_hash = repository.current_content_hash(resource.id)
    assert coalesced_hash is not None

    path.write_text("five", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=5)
    )
    path.write_text("six", encoding="utf-8")
    reconciler.handle_event(
        workspace.id, FilesystemEvent(FilesystemEventKind.MODIFIED, path, source_sequence=4)
    )
    assert repository.current_content_hash(resource.id) != coalesced_hash

    path.unlink()
    reconciler.request_full_reconciliation(workspace.id)
    assert repository.list_current_resources(workspace.id) == []
