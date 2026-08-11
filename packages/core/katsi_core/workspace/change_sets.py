"""Durable immutable Change Set submission and lifecycle history."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    ChangeSetTransition,
    Operation,
)
from katsi_core.workspace.errors import ConflictError, InvalidTransitionError

_OPERATION_ADAPTER = TypeAdapter(Operation)


class ChangeSetService:
    """Persists frozen proposals and append-only lifecycle transitions."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def submit(self, change_set: ChangeSet) -> ChangeSet:
        """Store a new immutable proposal or return its idempotent predecessor."""
        existing_id: UUID | None = None
        with self._database.connection() as connection, write_transaction(connection):
            existing = connection.execute(
                "SELECT id FROM change_sets WHERE workspace_id = ? AND idempotency_key = ?",
                (str(change_set.workspace_id), change_set.idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_id = UUID(existing["id"])
            else:
                connection.execute(
                    "INSERT INTO change_sets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(change_set.id),
                        str(change_set.workspace_id),
                        str(change_set.author_id),
                        change_set.title,
                        change_set.idempotency_key,
                        change_set.risk.value,
                        change_set.status.value,
                        None,
                        change_set.created_at.isoformat(),
                    ),
                )
                for dependency in change_set.dependencies:
                    connection.execute(
                        "INSERT INTO change_set_dependencies VALUES (?, ?, ?, ?, ?)",
                        (
                            str(change_set.id),
                            str(dependency.resource_id),
                            str(dependency.expected_version_id)
                            if dependency.expected_version_id
                            else None,
                            dependency.expected_content_hash,
                            int(dependency.expected_absent),
                        ),
                    )
                for ordinal, operation in enumerate(change_set.operations):
                    connection.execute(
                        "INSERT INTO change_set_operations VALUES (?, ?, ?, ?, ?)",
                        (
                            str(uuid4()),
                            str(change_set.id),
                            ordinal,
                            operation.kind,
                            json.dumps(operation.model_dump(mode="json"), sort_keys=True),
                        ),
                    )
        if existing_id is None:
            return change_set
        stored = self.get(existing_id)
        assert stored is not None
        return stored

    def get(self, change_set_id: UUID) -> ChangeSet | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM change_sets WHERE id = ?", (str(change_set_id),)
            ).fetchone()
            if row is None:
                return None
            dependencies = connection.execute(
                "SELECT * FROM change_set_dependencies WHERE change_set_id = ? ORDER BY resource_id",
                (str(change_set_id),),
            ).fetchall()
            operations = connection.execute(
                "SELECT operation_json FROM change_set_operations WHERE change_set_id = ? ORDER BY ordinal",
                (str(change_set_id),),
            ).fetchall()
        from katsi_core.workspace.contracts import ResourceDependency, RiskClass

        return ChangeSet(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            author_id=UUID(row["author_id"]),
            title=row["title"],
            idempotency_key=row["idempotency_key"],
            risk=RiskClass(row["risk"]),
            status=ChangeSetStatus(row["status"]),
            successor_id=UUID(row["successor_id"]) if row["successor_id"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            dependencies=tuple(
                ResourceDependency(
                    resource_id=UUID(item["resource_id"]),
                    expected_version_id=UUID(item["expected_version_id"])
                    if item["expected_version_id"]
                    else None,
                    expected_content_hash=item["expected_content_hash"],
                    expected_absent=bool(item["expected_absent"]),
                )
                for item in dependencies
            ),
            operations=tuple(
                _OPERATION_ADAPTER.validate_python(json.loads(item["operation_json"]))
                for item in operations
            ),
        )

    def revise(self, predecessor_id: UUID, successor: ChangeSet) -> ChangeSet:
        """Create a frozen successor and link it once to its predecessor."""
        predecessor = self.get(predecessor_id)
        if predecessor is None or predecessor.workspace_id != successor.workspace_id:
            raise ConflictError(
                "successor must revise an existing Change Set in the same workspace"
            )
        if predecessor.successor_id is not None:
            raise ConflictError(f"Change Set already has a successor: {predecessor_id}")
        self.submit(successor)
        with self._database.connection() as connection, write_transaction(connection):
            updated = connection.execute(
                "UPDATE change_sets SET successor_id = ? WHERE id = ? AND successor_id IS NULL",
                (str(successor.id), str(predecessor_id)),
            )
            if updated.rowcount != 1:
                raise ConflictError(f"Change Set changed concurrently: {predecessor_id}")
        return successor

    def transition(
        self,
        change_set_id: UUID,
        to_status: ChangeSetStatus,
        actor_id: UUID | None = None,
        evidence: dict[str, str] | None = None,
    ) -> ChangeSetTransition:
        current = self.get(change_set_id)
        if current is None:
            raise ConflictError(f"unknown Change Set: {change_set_id}")
        try:
            transition = ChangeSetTransition(
                id=uuid4(),
                change_set_id=change_set_id,
                from_status=current.status,
                to_status=to_status,
                actor_id=actor_id,
                occurred_at=datetime.now(UTC),
                evidence=evidence or {},
            )
        except ValueError as error:
            raise InvalidTransitionError(str(error)) from error
        with self._database.connection() as connection, write_transaction(connection):
            updated = connection.execute(
                "UPDATE change_sets SET status = ? WHERE id = ? AND status = ?",
                (to_status.value, str(change_set_id), current.status.value),
            )
            if updated.rowcount != 1:
                raise ConflictError(f"Change Set changed concurrently: {change_set_id}")
            connection.execute(
                "INSERT INTO change_set_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(transition.id),
                    str(change_set_id),
                    current.status.value,
                    to_status.value,
                    str(actor_id) if actor_id else None,
                    transition.occurred_at.isoformat(),
                    json.dumps(transition.evidence, sort_keys=True),
                ),
            )
        return transition

    def history(self, change_set_id: UUID) -> tuple[ChangeSetTransition, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM change_set_transitions WHERE change_set_id = ? ORDER BY occurred_at, id",
                (str(change_set_id),),
            ).fetchall()
        return tuple(
            ChangeSetTransition(
                id=UUID(row["id"]),
                change_set_id=UUID(row["change_set_id"]),
                from_status=ChangeSetStatus(row["from_status"]),
                to_status=ChangeSetStatus(row["to_status"]),
                actor_id=UUID(row["actor_id"]) if row["actor_id"] else None,
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                evidence=json.loads(row["evidence_json"]),
            )
            for row in rows
        )
