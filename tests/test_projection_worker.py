"""Tests for durable, idempotent projection-outbox delivery."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from katsi_core.config import ProjectionWorkerSettings, SQLiteSettings
from katsi_core.store import (
    ProjectionWorker,
    WorkspaceRepository,
    WorkspaceSQLite,
    apply_migrations,
)
from katsi_core.workspace.contracts import WorkspaceEventKind


def _repository(tmp_path: Path) -> tuple[WorkspaceRepository, UUID, ProjectionWorker]:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=1)
    repository = WorkspaceRepository(database)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = repository.register_workspace(root, "Workspace")
    worker = ProjectionWorker(database, ProjectionWorkerSettings(batch_size=2))
    return repository, workspace.id, worker


def test_worker_delivers_ordered_entries_and_persists_offset(tmp_path: Path) -> None:
    repository, workspace_id, worker = _repository(tmp_path)
    for state_version in range(1, 4):
        repository.append_event(
            workspace_id,
            state_version,
            WorkspaceEventKind.RESOURCE_UPDATED,
            projection_payloads={"graph": {"state_version": str(state_version)}},
        )
    delivered: list[str] = []

    assert (
        worker.run(
            workspace_id, "graph", lambda entry: delivered.append(entry.payload["state_version"])
        )
        == 2
    )
    assert delivered == ["1", "2"]
    assert worker.offset(workspace_id, "graph").outbox_id == 2
    assert (
        worker.run(
            workspace_id, "graph", lambda entry: delivered.append(entry.payload["state_version"])
        )
        == 1
    )
    assert delivered == ["1", "2", "3"]
    assert (
        worker.run(
            workspace_id, "graph", lambda entry: delivered.append(entry.payload["state_version"])
        )
        == 0
    )


def test_worker_does_not_advance_offset_when_projection_fails(tmp_path: Path) -> None:
    repository, workspace_id, worker = _repository(tmp_path)
    repository.append_event(
        workspace_id,
        1,
        WorkspaceEventKind.RESOURCE_UPDATED,
        projection_payloads={"vector": {"action": "replace"}},
    )

    with pytest.raises(RuntimeError, match="projection failed"):
        worker.run(
            workspace_id,
            "vector",
            lambda _entry: (_ for _ in ()).throw(RuntimeError("projection failed")),
        )

    assert worker.offset(workspace_id, "vector").outbox_id == 0
    delivered: list[int] = []
    assert worker.run(workspace_id, "vector", lambda entry: delivered.append(entry.id)) == 1
    assert len(delivered) == 1
