"""Advisory Work Lease coordination without long-lived filesystem locks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from katsi_core.config import LeaseSettings
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    CapabilityOperationClass,
    RelativePath,
    WorkLease,
    WorkLeaseKind,
    WorkLeaseStatus,
    WorkspaceId,
)
from katsi_core.workspace.errors import AuthorizationDeniedError, ConflictError
from katsi_core.workspace.identity import IdentityService


def _now() -> datetime:
    return datetime.now(UTC)


class WorkLeaseService:
    """Creates visible, time-bounded advisory leases for active agent work."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        identities: IdentityService,
        settings: LeaseSettings,
        authorization: AuthorizationService | None = None,
    ) -> None:
        self._database = database
        self._identities = identities
        self._settings = settings
        self._authorization = authorization

    def acquire(
        self,
        workspace_id: WorkspaceId,
        holder_id: AgentIdentityId,
        task_description: str,
        resource_scope: tuple[RelativePath, ...] = (),
    ) -> WorkLease:
        self._require_active_identity(holder_id)
        self._require_lease_capability(holder_id, workspace_id)
        acquired_at = _now()
        lease = WorkLease(
            id=uuid4(),
            workspace_id=workspace_id,
            holder_id=holder_id,
            kind=WorkLeaseKind.ADVISORY,
            task_description=task_description,
            resource_scope=resource_scope,
            acquired_at=acquired_at,
            expires_at=acquired_at + timedelta(seconds=self._settings.advisory_ttl_seconds),
        )
        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, acquired_at)
            connection.execute(
                "INSERT INTO work_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    None,
                ),
            )
        return lease

    def renew(
        self,
        lease_id: UUID,
        holder_id: AgentIdentityId,
        expected_expires_at: datetime,
    ) -> WorkLease:
        """Renew only the unchanged active lease held by the requesting identity."""
        self._require_active_identity(holder_id)
        now = _now()
        new_expiry = now + timedelta(seconds=self._settings.advisory_ttl_seconds)
        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)
            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Work Lease: {lease_id}")
            if (
                row["holder_id"] != str(holder_id)
                or row["status"] != WorkLeaseStatus.ACTIVE.value
                or row["expires_at"] != expected_expires_at.isoformat()
            ):
                raise ConflictError("Work Lease renewal conflicts with current state")
            connection.execute(
                "UPDATE work_leases SET expires_at = ? WHERE id = ?",
                (new_expiry.isoformat(), str(lease_id)),
            )
            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
        return self._from_row(row)

    def release(self, lease_id: UUID, holder_id: AgentIdentityId) -> WorkLease:
        self._require_active_identity(holder_id)
        now = _now()
        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)
            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Work Lease: {lease_id}")
            if row["holder_id"] != str(holder_id) or row["status"] != WorkLeaseStatus.ACTIVE.value:
                raise ConflictError("only the active lease holder may release this Work Lease")
            connection.execute(
                "UPDATE work_leases SET status = ?, released_at = ? WHERE id = ?",
                (WorkLeaseStatus.RELEASED.value, now.isoformat(), str(lease_id)),
            )
            row = connection.execute(
                "SELECT * FROM work_leases WHERE id = ?", (str(lease_id),)
            ).fetchone()
        return self._from_row(row)

    def active_for_workspace(self, workspace_id: WorkspaceId) -> list[WorkLease]:
        now = _now()
        with self._database.connection() as connection, write_transaction(connection):
            self._expire_due(connection, now)
            rows = connection.execute(
                "SELECT * FROM work_leases WHERE workspace_id = ? AND status = ? ORDER BY acquired_at, id",
                (str(workspace_id), WorkLeaseStatus.ACTIVE.value),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _expire_due(connection: object, now: datetime) -> None:
        connection.execute(
            "UPDATE work_leases SET status = ? WHERE status = ? AND expires_at <= ?",
            (WorkLeaseStatus.EXPIRED.value, WorkLeaseStatus.ACTIVE.value, now.isoformat()),
        )

    def _require_active_identity(self, identity_id: AgentIdentityId) -> None:
        identity = self._identities.get_identity(identity_id)
        if identity is None or not identity.active:
            raise AuthorizationDeniedError("identity is not active")

    def _require_lease_capability(
        self, identity_id: AgentIdentityId, workspace_id: WorkspaceId
    ) -> None:
        """Require that the actor has LEASE capability for the workspace.

        This checks:
        - Identity must exist and be active (not revoked)
        - Must have an active capability grant for LEASE operations
        - Grant must not be expired or revoked
        - Must be authorized for the specific workspace

        Raises AuthorizationDeniedError with specific error messages for each failure case.
        """
        # If no authorization service is provided, skip capability check (backward compatibility)
        if self._authorization is None:
            return

        # Get the agent identity - this checks existence and active/revoked status
        identity = self._identities.get_identity(identity_id)
        if identity is None:
            raise AuthorizationDeniedError("Agent identity not found")
        if not identity.active:
            raise AuthorizationDeniedError("Agent identity is not active")
        if identity.revoked_at is not None:
            raise AuthorizationDeniedError("Agent identity has been revoked")

        # Get active capability grant for LEASE operations in this workspace
        grant = self._authorization._get_active_capability_grant(
            identity_id, workspace_id, CapabilityOperationClass.LEASE
        )
        if grant is None:
            raise AuthorizationDeniedError(
                "No active capability grant for LEASE operations in this workspace"
            )

        # Check grant expiration
        if grant.expires_at and grant.expires_at < _now():
            raise AuthorizationDeniedError("Capability grant for LEASE operations has expired")

        # Check grant revocation
        if grant.revoked_at is not None:
            raise AuthorizationDeniedError("Capability grant for LEASE operations has been revoked")

        # Verify LEASE operation class is in the grant
        if CapabilityOperationClass.LEASE not in grant.operation_classes:
            raise AuthorizationDeniedError("LEASE operation class not included in capability grant")

    @staticmethod
    def _from_row(row: object) -> WorkLease:
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
