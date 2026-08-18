"""Durable Action Journal with planning entries before mutation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    ActionOutcome,
    ActionOutcomeId,
    ActionOutcomeStatus,
    ChangeSetId,
    Operation,
)


class ActionJournalService:
    """Append-only record of governed mutations with recovery data."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def create_planning_entry(
        self,
        change_set_id: ChangeSetId,
        operations: tuple[Operation, ...],
        affected_hashes: dict[str, str],
        preimages: dict[str, bytes],
    ) -> ActionOutcome:
        """Record planning entry before any mutation with recovery plan."""
        now = datetime.now(UTC)

        # Build recovery plan with all necessary data for rollback/recovery
        recovery_plan = self._build_recovery_plan(operations, affected_hashes, preimages)

        # Build plan with operation details
        plan = {
            "operations": [
                {
                    "kind": op.kind,
                    "path": op.path,
                    "byte_count": op.byte_count,
                    **op.model_dump(mode="json", exclude={"kind", "path", "byte_count"}),
                }
                for op in operations
            ],
            "affected_hashes": affected_hashes,
            "timestamp": now.isoformat(),
        }

        outcome_id = uuid4()
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO action_journal VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(outcome_id),
                    str(change_set_id),
                    ActionOutcomeStatus.VERIFIED.value,
                    json.dumps(plan, sort_keys=True),
                    json.dumps(recovery_plan, sort_keys=True),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

        return ActionOutcome(
            id=outcome_id,
            change_set_id=change_set_id,
            status=ActionOutcomeStatus.VERIFIED,
            occurred_at=now,
            receipt={"action_journal_id": str(outcome_id)},
        )

    def record_step(
        self,
        outcome_id: ActionOutcomeId,
        step_description: str,
        step_data: dict[str, str],
    ) -> ActionOutcome:
        """Record each operation step durably."""
        now = datetime.now(UTC)
        with self._database.connection() as connection, write_transaction(connection):
            # Get current journal entry
            row = connection.execute(
                "SELECT * FROM action_journal WHERE id = ?", (str(outcome_id),)
            ).fetchone()

            if row is None:
                raise KeyError(f"Action outcome not found: {outcome_id}")

            plan = json.loads(row["plan_json"])
            plan["steps"] = plan.get("steps", [])
            plan["steps"].append(
                {
                    "description": step_description,
                    "data": step_data,
                    "timestamp": now.isoformat(),
                }
            )

            connection.execute(
                "UPDATE action_journal SET plan_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(plan, sort_keys=True), now.isoformat(), str(outcome_id)),
            )

            row = connection.execute(
                "SELECT * FROM action_journal WHERE id = ?", (str(outcome_id),)
            ).fetchone()

        return self._from_row(row)

    def get_outcome(self, outcome_id: ActionOutcomeId) -> ActionOutcome | None:
        """Retrieve action outcome by ID."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM action_journal WHERE id = ?", (str(outcome_id),)
            ).fetchone()

            if row is None:
                return None

            return self._from_row(row)

    def update_status(
        self,
        outcome_id: ActionOutcomeId,
        new_status: ActionOutcomeStatus,
        receipt: dict[str, str] | None = None,
    ) -> ActionOutcome:
        """Update the status of an action outcome."""
        now = datetime.now(UTC)
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT * FROM action_journal WHERE id = ?", (str(outcome_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"Action outcome not found: {outcome_id}")
            current_receipt = json.loads(row["recovery_json"])
            if receipt:
                current_receipt.update(receipt)

            connection.execute(
                "UPDATE action_journal SET status = ?, recovery_json = ?, updated_at = ? WHERE id = ?",
                (
                    new_status.value,
                    json.dumps(current_receipt, sort_keys=True),
                    now.isoformat(),
                    str(outcome_id),
                ),
            )

            row = connection.execute(
                "SELECT * FROM action_journal WHERE id = ?", (str(outcome_id),)
            ).fetchone()

        return self._from_row(row)

    def get_by_change_set(self, change_set_id: ChangeSetId) -> list[ActionOutcome]:
        """Get all action outcomes for a change set."""
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM action_journal WHERE change_set_id = ? ORDER BY created_at",
                (str(change_set_id),),
            ).fetchall()

        return [self._from_row(row) for row in rows]

    @staticmethod
    def _build_recovery_plan(
        operations: tuple[Operation, ...],
        affected_hashes: dict[str, str],
        preimages: dict[str, bytes],
    ) -> dict[str, str]:
        """Build recovery plan with rollback instructions."""
        recovery_plan = {
            "rollback_operations": [],
            "preimage_locations": {},
            "hash_verification": affected_hashes,
        }

        for op in operations:
            rollback_op = {
                "kind": f"rollback_{op.kind}",
                "path": op.path,
                "restore_from": preimages.get(op.path, "quarantine"),
            }
            recovery_plan["rollback_operations"].append(rollback_op)

        return recovery_plan

    @staticmethod
    def _from_row(row: object) -> ActionOutcome:
        return ActionOutcome(
            id=UUID(row["id"]),
            change_set_id=UUID(row["change_set_id"]),
            status=ActionOutcomeStatus(row["status"]),
            occurred_at=datetime.fromisoformat(row["created_at"]),
            receipt=json.loads(row["recovery_json"]),
        )
