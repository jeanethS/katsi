from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import (
    OpenWork,
    OpenWorkStatus,
    WorkspaceRecord,
    WorkspaceRecordKind,
    WorkspaceRecordStatus,
)
from katsi_core.workspace.errors import InvalidTransitionError
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.records import WorkspaceRecordService


def test_durable_records_and_open_work_preserve_lifecycle_history(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 2)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    author = identities.register("Agent", "test")
    service = WorkspaceRecordService(database, identities)
    timestamp = datetime.now(UTC)
    decision = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.DECISION,
        text="Use SQLite as private authority.",
        created_at=timestamp,
        updated_at=timestamp,
    )
    blocker = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.BLOCKER,
        text="Await owner approval.",
        created_at=timestamp,
        updated_at=timestamp,
    )
    question = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.OPEN_QUESTION,
        text="Which verifier applies?",
        created_at=timestamp,
        updated_at=timestamp,
    )
    for record in (decision, blocker, question):
        service.publish_record(record)
    service.transition_record(decision.id, author.id, WorkspaceRecordStatus.VERIFIED)
    assert {record.kind for record in service.list_records(workspace.id)} == {
        WorkspaceRecordKind.DECISION,
        WorkspaceRecordKind.BLOCKER,
        WorkspaceRecordKind.OPEN_QUESTION,
    }
    assert (
        next(
            record for record in service.list_records(workspace.id) if record.id == decision.id
        ).status
        is WorkspaceRecordStatus.VERIFIED
    )

    work = OpenWork(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        description="Implement workspace brief assembly.",
        created_at=timestamp,
        updated_at=timestamp,
    )
    service.create_open_work(work)
    service.transition_open_work(work.id, author.id, OpenWorkStatus.BLOCKED)
    service.transition_open_work(work.id, author.id, OpenWorkStatus.OPEN)
    assert service.list_open_work(workspace.id)[0].status is OpenWorkStatus.OPEN
    with pytest.raises(InvalidTransitionError):
        service.transition_record(decision.id, author.id, WorkspaceRecordStatus.OPEN)


def test_schema_two_upgrades_an_existing_schema_one_database(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
        apply_migrations(connection, 2)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert {"workspace_records", "workspace_record_transitions", "open_work_transitions"} <= tables
