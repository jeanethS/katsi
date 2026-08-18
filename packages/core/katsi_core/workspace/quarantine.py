"""Quarantine and restore without permanent deletion and history preservation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.recovery_store import RecoveryBlobStore
from katsi_core.workspace.staging import AdjacentStagingManager


class QuarantineRecord:
    """Record of a quarantined artifact with history preservation."""

    def __init__(
        self,
        id: UUID,
        original_path: str,
        quarantine_path: str,
        quarantine_timestamp: datetime,
        action_history: list[dict[str, str]],
        content_hash: str | None = None,
    ):
        self.id = id
        self.original_path = original_path
        self.quarantine_path = quarantine_path
        self.quarantine_timestamp = quarantine_timestamp
        self.action_history = action_history
        self.content_hash = content_hash


class QuarantineService:
    """Quarantine and restore operations without permanent deletion."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        recovery_store: RecoveryBlobStore,
        staging_manager: AdjacentStagingManager,
    ) -> None:
        self._database = database
        self._recovery_store = recovery_store
        self._staging_manager = staging_manager

    def quarantine_file(
        self,
        file_path: Path,
        preserve_history: bool = True,
        store_content: bool = True,
    ) -> QuarantineRecord:
        """Quarantine file without permanent deletion, preserving history."""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        quarry_id = uuid4()
        quarantine_timestamp = datetime.now(UTC)

        # Create quarantine directory
        quarantine_dir = file_path.parent / ".katsi-quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        # Generate quarantine path
        quarantine_path = quarantine_dir / f"{file_path.name}.quarantine-{quarry_id.hex[:8]}"

        # Build action history
        action_history = []
        if preserve_history:
            action_history = self._collect_file_history(file_path)

        # Store content in recovery blob store if requested
        content_hash = None
        if store_content:
            with open(file_path, "rb") as f:
                content = f.read()
                content_hash = str(self._recovery_store.store(content))

        # Stage and move to quarantine
        try:
            # Stage the quarantine operation
            self._staging_manager.stage_file_copy(file_path, quarantine_path)

            # Record quarantine in database
            with self._database.connection() as connection, write_transaction(connection):
                connection.execute(
                    """INSERT INTO quarantine_records
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(quarry_id),
                        str(file_path),
                        str(quarantine_path),
                        quarantine_timestamp.isoformat(),
                        content_hash,
                        json.dumps(action_history, sort_keys=True),
                        datetime.now(UTC).isoformat(),
                    ),
                )

            # Atomic move to quarantine
            self._staging_manager.atomic_replace(
                self._staging_manager.get_stage_path(quarantine_path),
                quarantine_path,
            )

            # Clean up staging
            self._staging_manager.cleanup_stage(quarantine_path)

        except Exception:
            # Clean up on failure
            if quarantine_path.exists():
                quarantine_path.unlink()
            raise

        return QuarantineRecord(
            id=quarry_id,
            original_path=str(file_path),
            quarantine_path=str(quarantine_path),
            quarantine_timestamp=quarantine_timestamp,
            action_history=action_history,
            content_hash=content_hash,
        )

    def restore_quarantined_file(
        self,
        quarry_id: UUID,
        target_path: Path | None = None,
    ) -> dict[str, str]:
        """Restore quarantined file, preserving original/action history."""
        # Get quarantine record
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM quarantine_records WHERE id = ?",
                (str(quarry_id),),
            ).fetchone()

            if row is None:
                raise KeyError(f"Quarantine record not found: {quarry_id}")

            quarry_path = Path(row["quarantine_path"])
            original_path = Path(row["original_path"])
            action_history = json.loads(row["action_history_json"])
            content_hash = row["content_hash"]

        # Determine restore target
        restore_target = target_path or original_path

        # Verify quarantined file exists
        if not quarry_path.exists():
            raise FileNotFoundError(f"Quarantined file not found: {quarry_path}")

        # Restore using staging
        try:
            # Stage restore operation
            self._staging_manager.stage_file_copy(quarry_path, restore_target)

            # Atomic replace
            self._staging_manager.atomic_replace(
                self._staging_manager.get_stage_path(restore_target),
                restore_target,
            )

            # Clean up staging
            self._staging_manager.cleanup_stage(restore_target)

            # Update quarantine record with restore action
            self._add_restore_action(quarry_id, restore_target)

            return {
                "restored": "true",
                "quarantine_id": str(quarry_id),
                "restored_to": str(restore_target),
                "original_path": str(original_path),
                "action_history_count": str(len(action_history)),
                "content_preserved": "true" if content_hash else "false",
            }

        except Exception as e:
            return {
                "restored": "false",
                "error": str(e),
                "quarantine_id": str(quarry_id),
            }

    def list_quarantined_files(self) -> list[QuarantineRecord]:
        """List all quarantined files with their metadata."""
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM quarantine_records ORDER BY quarantine_timestamp",
            ).fetchall()

        return [
            QuarantineRecord(
                id=UUID(row["id"]),
                original_path=row["original_path"],
                quarantine_path=row["quarantine_path"],
                quarantine_timestamp=datetime.fromisoformat(row["quarantine_timestamp"]),
                action_history=json.loads(row["action_history_json"]),
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def delete_quarantine_record(
        self,
        quarry_id: UUID,
        preserve_content: bool = True,
    ) -> dict[str, str]:
        """Delete quarantine record (but preserve content by default)."""
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT * FROM quarantine_records WHERE id = ?",
                (str(quarry_id),),
            ).fetchone()

            if row is None:
                raise KeyError(f"Quarantine record not found: {quarry_id}")

            quarry_path = Path(row["quarantine_path"])
            content_hash = row["content_hash"]

            # Delete the physical quarantine file
            if quarry_path.exists():
                quarry_path.unlink()

            # Delete from database
            connection.execute(
                "DELETE FROM quarantine_records WHERE id = ?",
                (str(quarry_id),),
            )

        return {
            "deleted": "true",
            "quarantine_id": str(quarry_id),
            "content_preserved": "true" if preserve_content and content_hash else "false",
        }

    def _collect_file_history(self, file_path: Path) -> list[dict[str, str]]:
        """Collect file history for preservation."""
        # Placeholder for actual history collection
        # In a full implementation, this would query the workspace events log
        return [
            {
                "action": "original_state",
                "timestamp": datetime.now(UTC).isoformat(),
                "path": str(file_path),
            }
        ]

    def _add_restore_action(self, quarry_id: UUID, restore_path: Path) -> None:
        """Add restore action to quarantine record history."""
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT action_history_json FROM quarantine_records WHERE id = ?",
                (str(quarry_id),),
            ).fetchone()

            if row is None:
                return

            action_history = json.loads(row["action_history_json"])
            action_history.append(
                {
                    "action": "restore",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "restore_path": str(restore_path),
                }
            )

            connection.execute(
                """UPDATE quarantine_records
                   SET action_history_json = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(action_history, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    str(quarry_id),
                ),
            )

    def get_statistics(self) -> dict[str, int]:
        """Get statistics about quarantined files."""
        with self._database.connection() as connection:
            total_count = connection.execute("SELECT COUNT(*) FROM quarantine_records").fetchone()[
                0
            ]

            total_size = (
                connection.execute(
                    "SELECT SUM(quarantine_size) FROM quarantine_records"
                ).fetchone()[0]
                or 0
            )

        return {
            "total_quarantined": total_count,
            "total_size_bytes": total_size,
        }
