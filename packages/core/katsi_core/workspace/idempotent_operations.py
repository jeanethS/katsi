"""Idempotent operation requests with durable recording and resume capability."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.action_journal import ActionJournalService
from katsi_core.workspace.contracts import (
    ActionOutcome,
    ActionOutcomeId,
    ActionOutcomeStatus,
    ChangeSetId,
    Operation,
)
from katsi_core.workspace.errors import ConflictError


class IdempotentOperationService:
    """Idempotent operation requests with durable recording and resume capability."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        action_journal: ActionJournalService,
    ) -> None:
        self._database = database
        self._action_journal = action_journal

    def execute_operation_idempotent(
        self,
        operation: Operation,
        change_set_id: ChangeSetId,
        execution_id: UUID | None = None,
    ) -> tuple[ActionOutcomeId, dict[str, str]]:
        """Execute operation with idempotent result return and resume capability."""
        # Generate stable execution ID if not provided
        if execution_id is None:
            execution_id = uuid4()

        # Check for existing execution result
        existing_result = self._get_existing_result(execution_id, operation)
        if existing_result is not None:
            return execution_id, existing_result

        # Record operation start
        self._record_operation_start(execution_id, change_set_id, operation)

        # Execute operation (placeholder for actual execution)
        try:
            result = self._execute_operation(operation)

            # Record successful completion
            self._record_operation_completion(execution_id, result)

            return execution_id, result

        except Exception as e:
            # Record failure
            self._record_operation_failure(execution_id, str(e))
            raise

    def resume_operation(
        self,
        execution_id: UUID,
    ) -> tuple[ActionOutcomeStatus, dict[str, str] | None]:
        """Resume or return existing idempotent result."""
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM idempotent_operations
                   WHERE execution_id = ?""",
                (str(execution_id),),
            ).fetchone()

            if row is None:
                raise KeyError(f"Unknown operation execution: {execution_id}")

            status = ActionOutcomeStatus(row["status"])

            if status == ActionOutcomeStatus.VERIFIED:
                result = json.loads(row["result_json"]) if row["result_json"] else None
                return status, result
            elif status == ActionOutcomeStatus.RECOVERY_REQUIRED:
                return status, json.loads(row["error_json"]) if row["error_json"] else None
            else:
                return status, None

    def record_step(
        self,
        execution_id: UUID,
        step_description: str,
        step_data: dict[str, str],
    ) -> None:
        """Record each operation step durably."""
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            # Get existing steps
            row = connection.execute(
                "SELECT steps_json FROM idempotent_operations WHERE execution_id = ?",
                (str(execution_id),),
            ).fetchone()

            if row is None:
                raise KeyError(f"Unknown operation execution: {execution_id}")

            steps = json.loads(row["steps_json"]) if row["steps_json"] else []
            steps.append(
                {
                    "description": step_description,
                    "data": step_data,
                    "timestamp": now.isoformat(),
                }
            )

            connection.execute(
                "UPDATE idempotent_operations SET steps_json = ? WHERE execution_id = ?",
                (json.dumps(steps), str(execution_id)),
            )

    def _get_existing_result(
        self,
        execution_id: UUID,
        operation: Operation,
    ) -> dict[str, str] | None:
        """Check for existing idempotent result."""
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM idempotent_operations
                   WHERE execution_id = ?""",
                (str(execution_id),),
            ).fetchone()

            if row is None:
                return None

            # Verify operation matches
            stored_operation = json.loads(row["operation_json"])
            if (
                stored_operation["kind"] != operation.kind
                or stored_operation["path"] != operation.path
            ):
                raise ConflictError("Operation mismatch for execution ID")

            status = ActionOutcomeStatus(row["status"])

            if status == ActionOutcomeStatus.VERIFIED and row["result_json"]:
                return json.loads(row["result_json"])

            return None

    def _record_operation_start(
        self,
        execution_id: UUID,
        change_set_id: ChangeSetId,
        operation: Operation,
    ) -> ActionOutcome:
        """Record operation start durably."""
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                """INSERT INTO idempotent_operations
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(execution_id),
                    str(change_set_id),
                    operation.kind,
                    json.dumps(operation.model_dump(mode="json"), sort_keys=True),
                    ActionOutcomeStatus.VERIFIED.value,
                    None,
                    None,
                    "[]",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

        return ActionOutcome(
            id=execution_id,
            change_set_id=change_set_id,
            status=ActionOutcomeStatus.VERIFIED,
            occurred_at=now,
            receipt={"execution_id": str(execution_id)},
        )

    def _record_operation_completion(
        self,
        execution_id: UUID,
        result: dict[str, str],
    ) -> None:
        """Record operation completion."""
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                """UPDATE idempotent_operations
                   SET status = ?, result_json = ?, updated_at = ?
                   WHERE execution_id = ?""",
                (
                    ActionOutcomeStatus.VERIFIED.value,
                    json.dumps(result, sort_keys=True),
                    now.isoformat(),
                    str(execution_id),
                ),
            )

    def _record_operation_failure(
        self,
        execution_id: UUID,
        error_message: str,
    ) -> None:
        """Record operation failure."""
        now = datetime.now(UTC)

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                """UPDATE idempotent_operations
                   SET status = ?, error_json = ?, updated_at = ?
                   WHERE execution_id = ?""",
                (
                    ActionOutcomeStatus.RECOVERY_REQUIRED.value,
                    json.dumps({"error": error_message}, sort_keys=True),
                    now.isoformat(),
                    str(execution_id),
                ),
            )

    def _execute_operation(self, operation: Operation) -> dict[str, str]:
        """Execute operation (placeholder for actual execution)."""
        # This would contain the actual operation execution logic
        # For now, return a placeholder result
        return {
            "kind": operation.kind,
            "path": operation.path,
            "status": "success",
            "timestamp": datetime.now(UTC).isoformat(),
        }
