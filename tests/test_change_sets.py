from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    CreateFileOperation,
    RiskClass,
)
from katsi_core.workspace.errors import InvalidTransitionError
from katsi_core.workspace.identity import IdentityService

HASH = "a" * 64


def test_submission_is_idempotent_and_transitions_are_append_only(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")
    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Create brief",
        idempotency_key="brief-v1",
        dependencies=(),
        operations=(CreateFileOperation(path="brief.md", byte_count=1, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    assert service.submit(proposal) == proposal
    duplicate = proposal.model_copy(update={"id": uuid4(), "title": "ignored"})
    assert service.submit(duplicate) == proposal

    transition = service.transition(proposal.id, ChangeSetStatus.VALIDATED, author.id)
    assert transition.from_status is ChangeSetStatus.PROPOSED
    assert service.get(proposal.id).status is ChangeSetStatus.VALIDATED  # type: ignore[union-attr]
    assert service.history(proposal.id) == (transition,)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal.id, ChangeSetStatus.VERIFIED, author.id)
