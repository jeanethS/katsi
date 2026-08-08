from pathlib import Path

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.intent import IntentService


def test_active_intent_is_versioned_and_compare_and_set(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 3)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    intents = IntentService(database)
    assert intents.activate(workspace.id, "Ship coordination", expected_version=0) == 1
    assert intents.get(workspace.id) == ("Ship coordination", 1)
    with pytest.raises(ConflictError):
        intents.activate(workspace.id, "Replace goal", expected_version=0)
