"""Exclusive write-set leases with transactional overlap prevention."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from katsi_core.config import LeaseSettings
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    ChangeSetId,
    RelativePath,
    WorkLease,
    WorkLeaseKind,
    WorkLeaseStatus,
    WorkspaceId,
)
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.identity import IdentityService


class ExclusiveLeaseService:
    """Short-duration exclusive leases with overlap detection and Change Set correlation."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        identities: IdentityService,
        settings: LeaseSettings,
    ) -> None:
        self._database = database
        self._identities = identities
        self._settings = settings

    def acquire_exclusive(
        self,
        workspace_id: WorkspaceId,
        holder_id: AgentIdentityId,
        change_set_id: ChangeSetId,
        task_description: str,
        write_set: tuple[RelativePath, ...],
    ) -> WorkLease:
        """Acquire exclusive lease over write-set with overlap detection."""
        self._require_active_identity(holder_id)
        now = datetime.now(UTC)

        # Check for overlap with existing exclusive leases
        conflicts = self._detect_conflicts(workspace_id, write_set)
        if conflicts:
            conflict_details = [
                f"Lease {conflict['lease_id']} holder {conflict['holder_id']} "
                f"conflicts on paths: {conflict['conflicting_paths']}"
                for conflict in conflicts
            ]
            raise ConflictError(
                f"Write-set conflicts with {len(conflicts)} existing lease(s): "
                + "; ".join(conflict_details)
            )

        # Create exclusive lease
        lease = WorkLease(
            id=uuid4(),
            workspace_id=workspace_id,
            holder_id=holder_id,
            kind=WorkLeaseKind.EXCLUSIVE,
            task_description=task_description,
            resource_scope=write_set,
            acquired_at=now,
            expires_at=now + timedelta(seconds=self._settings.exclusive_ttl_seconds),
        )

        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)

            # Insert lease with correlation to change set
            connection.execute(
                """INSERT INTO work_leases
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(lease.id),
                    str(lease.workspace_id),
                    str(lease.holder_id),
                    lease.kind.value,
                    lease.status.value,
                    lease.task_description,
                    json.dumps(lease.resource_scope),
                    lease.acquired_at.isoformat(),
                    lease.expires_at.isoformat(),
                    str(change_set_id),  # Store in released_at as correlation
                ),
            )

        return lease

    def renew_exclusive(
        self,
        lease_id: UUID,
        holder_id: AgentIdentityId,
        expected_expires_at: datetime,
    ) -> WorkLease:
        """Renew exclusive lease with conflict recheck."""
        self._require_active_identity(holder_id)
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)

            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()

            if row is None:
                raise KeyError(f"Unknown exclusive lease: {lease_id}")

            if (
                row["holder_id"] != str(holder_id)
                or row["status"] != WorkLeaseStatus.ACTIVE.value
                or row["kind"] != WorkLeaseKind.EXCLUSIVE.value
                or row["expires_at"] != expected_expires_at.isoformat()
            ):
                raise ConflictError("Exclusive lease renewal conflicts with current state")

            # Recheck for conflicts on renewal
            write_set = tuple(json.loads(row["resource_scope_json"]))
            workspace_id = UUID(row["workspace_id"])

            conflicts = self._detect_conflicts(workspace_id, write_set, exclude_lease=lease_id)
            if conflicts:
                raise ConflictError(
                    f"Cannot renew: new conflicts detected with {len(conflicts)} lease(s)"
                )

            # Update expiry
            new_expiry = now + timedelta(seconds=self._settings.exclusive_ttl_seconds)
            connection.execute(
                "UPDATE work_leases SET expires_at = ? WHERE id = ?",
                (new_expiry.isoformat(), str(lease_id)),
            )

            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()

        return self._from_row(row)

    def release_exclusive(
        self,
        lease_id: UUID,
        holder_id: AgentIdentityId,
        terminal_outcome: bool,
        recovery_required: bool = False,
    ) -> WorkLease:
        """Release exclusive lease only after terminal or recovery-required outcome."""
        self._require_active_identity(holder_id)

        if not (terminal_outcome or recovery_required):
            raise ValueError(
                "Exclusive lease may only be released after terminal outcome "
                "or when recovery is required"
            )

        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)

            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()

            if row is None:
                raise KeyError(f"Unknown exclusive lease: {lease_id}")

            if row["holder_id"] != str(holder_id):
                raise ConflictError("Only the lease holder may release this exclusive lease")

            if row["kind"] != WorkLeaseKind.EXCLUSIVE.value:
                raise ConflictError("Can only release exclusive leases")

            if row["status"] != WorkLeaseStatus.ACTIVE.value:
                raise ConflictError("Lease is not active")

            connection.execute(
                "UPDATE work_leases SET status = ?, released_at = ? WHERE id = ?",
                (WorkLeaseStatus.RELEASED.value, now.isoformat(), str(lease_id)),
            )

            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()

        return self._from_row(row)

    def get_active_exclusive_leases(
        self,
        workspace_id: WorkspaceId,
    ) -> list[WorkLease]:
        """Get all active exclusive leases for a workspace."""
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)

            rows = connection.execute(
                """SELECT * FROM work_leases
                   WHERE workspace_id = ?
                   AND kind = ?
                   AND status = ?
                   ORDER BY acquired_at, id""",
                (
                    str(workspace_id),
                    WorkLeaseKind.EXCLUSIVE.value,
                    WorkLeaseStatus.ACTIVE.value,
                ),
            ).fetchall()

        return [self._from_row(row) for row in rows]

    def check_conflicts(
        self,
        workspace_id: WorkspaceId,
        write_set: tuple[RelativePath, ...],
    ) -> list[dict[str, str]]:
        """Check if write-set would conflict with existing leases (non-mutating)."""
        return self._detect_conflicts(workspace_id, write_set)

    def _detect_conflicts(
        self,
        workspace_id: WorkspaceId,
        write_set: tuple[RelativePath, ...],
        exclude_lease: UUID | None = None,
    ) -> list[dict[str, str]]:
        """Detect overlapping write-sets with existing exclusive leases."""
        conflicts = []
        now = datetime.now(UTC)

        with self._database.connection() as connection:
            # Get all active exclusive leases for workspace
            rows = connection.execute(
                """SELECT * FROM work_leases
                   WHERE workspace_id = ?
                   AND kind = ?
                   AND status = ?
                   AND expires_at > ?""",
                (
                    str(workspace_id),
                    WorkLeaseKind.EXCLUSIVE.value,
                    WorkLeaseStatus.ACTIVE.value,
                    now.isoformat(),
                ),
            ).fetchall()

            for row in rows:
                lease_id = UUID(row["id"])

                # Skip the lease we're checking for renewal
                if exclude_lease and lease_id == exclude_lease:
                    continue

                existing_scope = set(json.loads(row["resource_scope_json"]))
                requested_scope = set(write_set)

                # Check for overlap
                overlap = existing_scope & requested_scope

                if overlap:
                    conflicts.append({
                        "lease_id": str(lease_id),
                        "holder_id": row["holder_id"],
                        "conflicting_paths": sorted(overlap),
                        "expires_at": row["expires_at"],
                    })

        return conflicts

    def _expire_due(self, connection: object, now: datetime) -> None:
        """Expire overdue leases."""
        connection.execute(
            """UPDATE work_leases
               SET status = ?
               WHERE status = ? AND expires_at <= ?""",
            (WorkLeaseStatus.EXPIRED.value, WorkLeaseStatus.ACTIVE.value, now.isoformat()),
        )

    def _require_active_identity(self, identity_id: AgentIdentityId) -> None:
        """Require that the identity is active."""
        identity = self._identities.get_identity(identity_id)
        if identity is None or not identity.active:
            raise ConflictError("Identity is not active")

    @staticmethod
    def _from_row(row: object) -> WorkLease:
        """Reconstruct WorkLease from database row."""
        return WorkLease(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            holder_id=UUID(row["holder_id"]),
            kind=WorkLeaseKind(row["kind"]),
            status=WorkLeaseStatus(row["status"]),
            task_description=row["task_description"],
            resource_scope=tuple(json.loads(row["resource_scope_json"])),
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            released_at=datetime.fromisoformat(row["released_at"]) if row["released_at"] else None,
        )