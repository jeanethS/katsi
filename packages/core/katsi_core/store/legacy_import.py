"""Read-only migration from legacy ``file_records.json`` state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from katsi_core.models import FileRecord
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import ResourceId, WorkspaceId

if TYPE_CHECKING:
    from katsi_core.media.registry import RepresentationRegistry


class LegacyFileRecordImporter:
    """Imports valid legacy records once without modifying the source JSON file."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        repository: WorkspaceRepository,
        representation_registry: RepresentationRegistry | None = None,
    ) -> None:
        self._database = database
        self._repository = repository
        if representation_registry is None:
            self._representation_migrator = None
        else:
            from katsi_core.media.migration import LegacyTextRepresentationMigrator

            self._representation_migrator = LegacyTextRepresentationMigrator(
                representation_registry
            )

    def import_file(
        self, legacy_path: Path, workspace_id: WorkspaceId, enrichment_fingerprint: str
    ) -> int:
        """Import valid records and return the number newly migrated."""
        raw = json.loads(legacy_path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("legacy file_records.json must contain an object")
        self._ensure_ledger()
        imported = 0
        for legacy_id, value in raw.items():
            try:
                record = FileRecord.model_validate(value)
            except ValidationError:
                continue
            if self._is_imported(workspace_id, legacy_id):
                continue
            workspace = self._repository.get_workspace(workspace_id)
            if workspace is None:
                raise ValueError(f"unknown workspace {workspace_id}")
            root = Path(workspace.root_path)
            try:
                relative_path = str(Path(record.path).resolve().relative_to(root.resolve()))
            except ValueError:
                continue
            version = self._repository.create_resource(
                workspace.id,
                workspace.state_version,
                relative_path,
                record.content_hash,
                record.size_bytes,
            )
            if not Path(record.path).exists():
                current_workspace = self._repository.get_workspace(workspace.id)
                resource = self._repository.get_resource(version.resource_id)
                assert current_workspace is not None and resource is not None
                self._repository.delete_resource(
                    workspace.id,
                    current_workspace.state_version,
                    resource.id,
                    resource.state_version,
                )
            self._persist_enrichment(record, enrichment_fingerprint)
            if self._representation_migrator is not None:
                self._representation_migrator.import_text(
                    legacy_id=legacy_id,
                    resource_version_id=version.id,
                    content_hash=record.content_hash,
                    text=record.summary or "",
                    created_at=record.last_indexed_at,
                )
            self._mark_imported(workspace.id, legacy_id, version.resource_id)
            imported += 1
        return imported

    def _ensure_ledger(self) -> None:
        with self._database.connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS legacy_file_record_imports (
                workspace_id TEXT NOT NULL, legacy_id TEXT NOT NULL, resource_id TEXT NOT NULL,
                PRIMARY KEY (workspace_id, legacy_id))"""
            )

    def _is_imported(self, workspace_id: WorkspaceId, legacy_id: str) -> bool:
        with self._database.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM legacy_file_record_imports WHERE workspace_id = ? AND legacy_id = ?",
                    (str(workspace_id), legacy_id),
                ).fetchone()
                is not None
            )

    def _mark_imported(
        self, workspace_id: WorkspaceId, legacy_id: str, resource_id: ResourceId
    ) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO legacy_file_record_imports VALUES (?, ?, ?)",
                (str(workspace_id), legacy_id, str(resource_id)),
            )

    def _persist_enrichment(self, record: FileRecord, fingerprint: str) -> None:
        if record.summary is None:
            return
        with self._database.connection() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO content_enrichments
                VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                (
                    record.content_hash,
                    fingerprint,
                    json.dumps({"summary": record.summary}),
                    "success",
                    None,
                ),
            )


class LegacyCleanupGuard:
    """Prevents irreversible legacy cleanup before authority has been verified."""

    @staticmethod
    def require_safe_cleanup(*, reconciliation_passed: bool, projections_validated: bool) -> None:
        if not reconciliation_passed or not projections_validated:
            raise ValueError(
                "legacy cleanup requires successful reconciliation and projection validation"
            )
