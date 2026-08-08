from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from katsi_core.config import LeaseSettings, SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.leases import WorkLeaseService


def test_advisory_leases_overlap_renew_release_and_expire(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 2)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    first = identities.register("First", "test")
    second = identities.register("Second", "test")
    service = WorkLeaseService(database, identities, LeaseSettings(advisory_ttl_seconds=30))

    first_lease = service.acquire(workspace.id, first.id, "Inspect docs", ("docs",))
    second_lease = service.acquire(workspace.id, second.id, "Edit docs", ("docs/readme.md",))
    assert {lease.id for lease in service.active_for_workspace(workspace.id)} == {
        first_lease.id,
        second_lease.id,
    }
    renewed = service.renew(first_lease.id, first.id, first_lease.expires_at)
    assert renewed.expires_at > first_lease.expires_at
    with pytest.raises(ConflictError):
        service.renew(first_lease.id, first.id, first_lease.expires_at)
    released = service.release(second_lease.id, second.id)
    assert released.released_at is not None

    with database.connection() as connection:
        connection.execute(
            "UPDATE work_leases SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), str(renewed.id)),
        )
    assert service.active_for_workspace(workspace.id) == []
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT status FROM work_leases WHERE id = ?", (str(renewed.id),)
            ).fetchone()[0]
            == "expired"
        )


def test_only_the_holder_can_release_an_advisory_lease(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 2)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    holder = identities.register("Holder", "test")
    other = identities.register("Other", "test")
    service = WorkLeaseService(database, identities, LeaseSettings())
    lease = service.acquire(workspace.id, holder.id, "Explore", ("src",))
    with pytest.raises(ConflictError):
        service.release(lease.id, other.id)
