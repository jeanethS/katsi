"""Projection and authoritative-store adapters."""

from katsi_core.store.enrichment_cache import EnrichmentCache
from katsi_core.store.legacy_import import LegacyCleanupGuard, LegacyFileRecordImporter
from katsi_core.store.projection_worker import (
    ProjectionOffset,
    ProjectionOutboxEntry,
    ProjectionWorker,
)
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
    "LegacyFileRecordImporter",
    "LegacyCleanupGuard",
    "EnrichmentCache",
    "ProjectionOffset",
    "ProjectionOutboxEntry",
    "ProjectionWorker",
    "WorkspaceRepository",
    "apply_migrations",
    "require_resource_versions",
    "require_workspace_version",
    "write_transaction",
]
