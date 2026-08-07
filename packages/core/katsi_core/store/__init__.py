"""Projection and authoritative-store adapters."""

from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import (
    require_resource_versions,
    require_workspace_version,
    write_transaction,
)

__all__ = [
    "WorkspaceSQLite",
    "WorkspaceRepository",
    "apply_migrations",
    "require_resource_versions",
    "require_workspace_version",
    "write_transaction",
]
