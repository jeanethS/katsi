"""Durable decisions, blockers, questions, and open-work lifecycle records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    OpenWork,
    OpenWorkStatus,
    OpenWorkTransition,
    WorkspaceId,
    WorkspaceRecord,
    WorkspaceRecordKind,
    WorkspaceRecordStatus,
    WorkspaceRecordTransition,
)
from katsi_core.workspace.errors import AuthorizationDeniedError, InvalidTransitionError
from katsi_core.workspace.identity import IdentityService


def _now() -> datetime:
    return datetime.now(UTC)


_RECORD_TRANSITIONS: dict[WorkspaceRecordStatus, frozenset[WorkspaceRecordStatus]] = {
    WorkspaceRecordStatus.OPEN: frozenset(
        {
            WorkspaceRecordStatus.VERIFIED,
            WorkspaceRecordStatus.RESOLVED,
            WorkspaceRecordStatus.DISMISSED,
        }
    ),
    WorkspaceRecordStatus.VERIFIED: frozenset(
        {WorkspaceRecordStatus.RESOLVED, WorkspaceRecordStatus.DISMISSED}
    ),
    WorkspaceRecordStatus.RESOLVED: frozenset(),
    WorkspaceRecordStatus.DISMISSED: frozenset(),
}
_OPEN_WORK_TRANSITIONS: dict[OpenWorkStatus, frozenset[OpenWorkStatus]] = {
    OpenWorkStatus.OPEN: frozenset(
        {OpenWorkStatus.BLOCKED, OpenWorkStatus.COMPLETED, OpenWorkStatus.CANCELLED}
    ),
    OpenWorkStatus.BLOCKED: frozenset(
        {OpenWorkStatus.OPEN, OpenWorkStatus.COMPLETED, OpenWorkStatus.CANCELLED}
    ),
    OpenWorkStatus.COMPLETED: frozenset(),
    OpenWorkStatus.CANCELLED: frozenset(),
}


class WorkspaceRecordService:
    """Keeps durable workspace coordination records append-only and attributable."""

    def __init__(self, database: WorkspaceSQLite, identities: IdentityService) -> None:
        self._database = database
        self._identities = identities

    def publish_record(self, record: WorkspaceRecord) -> WorkspaceRecord:
        self._require_active_identity(record.author_id)
        if record.status is not WorkspaceRecordStatus.OPEN:
            raise InvalidTransitionError("new workspace records must start open")
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO workspace_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.id),
                    str(record.workspace_id),
                    str(record.author_id),
                    record.kind.value,
                    record.text,
                    record.status.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def transition_record(
        self,
        record_id: UUID,
        actor_id: AgentIdentityId,
        to_status: WorkspaceRecordStatus,
        evidence: dict[str, str] | None = None,
    ) -> WorkspaceRecordTransition:
        self._require_active_identity(actor_id)
        timestamp = _now()
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT status FROM workspace_records WHERE id = ?", (str(record_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown workspace record: {record_id}")
            from_status = WorkspaceRecordStatus(row["status"])
            if to_status not in _RECORD_TRANSITIONS[from_status]:
                raise InvalidTransitionError(
                    f"invalid workspace record transition: {from_status} -> {to_status}"
                )
            transition = WorkspaceRecordTransition(
                id=uuid4(),
                record_id=record_id,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                occurred_at=timestamp,
                evidence=evidence or {},
            )
            connection.execute(
                "UPDATE workspace_records SET status = ?, updated_at = ? WHERE id = ?",
                (to_status.value, timestamp.isoformat(), str(record_id)),
            )
            connection.execute(
                "INSERT INTO workspace_record_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(transition.id),
                    str(record_id),
                    from_status.value,
                    to_status.value,
                    str(actor_id),
                    timestamp.isoformat(),
                    json.dumps(transition.evidence),
                ),
            )
        return transition

    def create_open_work(self, work: OpenWork) -> OpenWork:
        self._require_active_identity(work.author_id)
        if work.status is not OpenWorkStatus.OPEN:
            raise InvalidTransitionError("new open work must start open")
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO open_work VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(work.id),
                    str(work.workspace_id),
                    str(work.author_id),
                    work.description,
                    work.status.value,
                    work.created_at.isoformat(),
                    work.updated_at.isoformat(),
                ),
            )
        return work

    def transition_open_work(
        self,
        open_work_id: UUID,
        actor_id: AgentIdentityId,
        to_status: OpenWorkStatus,
        evidence: dict[str, str] | None = None,
    ) -> OpenWorkTransition:
        self._require_active_identity(actor_id)
        timestamp = _now()
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT status FROM open_work WHERE id = ?", (str(open_work_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown open work: {open_work_id}")
            from_status = OpenWorkStatus(row["status"])
            if to_status not in _OPEN_WORK_TRANSITIONS[from_status]:
                raise InvalidTransitionError(
                    f"invalid open-work transition: {from_status} -> {to_status}"
                )
            transition = OpenWorkTransition(
                id=uuid4(),
                open_work_id=open_work_id,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                occurred_at=timestamp,
                evidence=evidence or {},
            )
            connection.execute(
                "UPDATE open_work SET status = ?, updated_at = ? WHERE id = ?",
                (to_status.value, timestamp.isoformat(), str(open_work_id)),
            )
            connection.execute(
                "INSERT INTO open_work_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(transition.id),
                    str(open_work_id),
                    from_status.value,
                    to_status.value,
                    str(actor_id),
                    timestamp.isoformat(),
                    json.dumps(transition.evidence),
                ),
            )
        return transition

    def list_records(self, workspace_id: WorkspaceId) -> list[WorkspaceRecord]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_records WHERE workspace_id = ? ORDER BY created_at, id",
                (str(workspace_id),),
            ).fetchall()
        return [
            WorkspaceRecord(
                id=UUID(row["id"]),
                workspace_id=UUID(row["workspace_id"]),
                author_id=UUID(row["author_id"]),
                kind=WorkspaceRecordKind(row["kind"]),
                text=row["text"],
                status=WorkspaceRecordStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def list_open_work(self, workspace_id: WorkspaceId) -> list[OpenWork]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM open_work WHERE workspace_id = ? ORDER BY created_at, id",
                (str(workspace_id),),
            ).fetchall()
        return [
            OpenWork(
                id=UUID(row["id"]),
                workspace_id=UUID(row["workspace_id"]),
                author_id=UUID(row["author_id"]),
                description=row["description"],
                status=OpenWorkStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def _require_active_identity(self, identity_id: AgentIdentityId) -> None:
        identity = self._identities.get_identity(identity_id)
        if identity is None or not identity.active:
            raise AuthorizationDeniedError("identity is not active")
