"""MCP/owner API for Change Set lifecycle management."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    RiskClass,
)
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.staleness import StalenessService
from katsi_core.workspace.validation import ValidationService

logger = logging.getLogger(__name__)


class ChangeSetProposal:
    """A proposal for a new Change Set."""

    def __init__(
        self,
        workspace_id: UUID,
        author_id: UUID,
        title: str,
        idempotency_key: str,
        dependencies: tuple[dict, ...],
        operations: tuple[dict, ...],
        risk: RiskClass = RiskClass.LOW,
    ) -> None:
        self.workspace_id = workspace_id
        self.author_id = author_id
        self.title = title
        self.idempotency_key = idempotency_key
        self.dependencies = dependencies
        self.operations = operations
        self.risk = risk


class OwnerDecision:
    """Immutable owner decision evidence."""

    def __init__(
        self,
        decision_id: UUID,
        change_set_id: UUID,
        decision: str,  # "approved" or "rejected"
        actor_id: UUID,
        decided_at: datetime,
        reason: str | None = None,
        evidence: dict[str, str] | None = None,
    ) -> None:
        self.decision_id = decision_id
        self.change_set_id = change_set_id
        self.decision = decision
        self.actor_id = actor_id
        self.decided_at = decided_at
        self.reason = reason
        self.evidence = evidence or {}

    def to_dict(self) -> dict[str, object]:
        """Convert to serializable dictionary."""
        return {
            "decision_id": str(self.decision_id),
            "change_set_id": str(self.change_set_id),
            "decision": self.decision,
            "actor_id": str(self.actor_id),
            "decided_at": self.decided_at.isoformat(),
            "reason": self.reason,
            "evidence": self.evidence,
        }


class OwnerAPI:
    """
    MCP/owner API for Change Set lifecycle management.
    Provides endpoints for proposing, validating, reviewing, approving,
    rejecting, and superseding Change Sets without applying files.
    """

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database
        self._change_set_service = ChangeSetService(database)
        self._validation_service = ValidationService(database)
        self._staleness_service = StalenessService(database)
        self._authorization_service = AuthorizationService(database)

    def propose_change_set(self, proposal: ChangeSetProposal) -> ChangeSet:
        """
        Propose a new Change Set.
        Returns the created Change Set with PROPOSED status.
        """
        from katsi_core.workspace.contracts import ResourceDependency

        # Convert proposal data to contracts
        dependencies = tuple(
            ResourceDependency(
                resource_id=UUID(dep["resource_id"]),
                expected_version_id=UUID(dep["expected_version_id"])
                if dep.get("expected_version_id")
                else None,
                expected_content_hash=dep.get("expected_content_hash"),
                expected_absent=dep.get("expected_absent", False),
            )
            for dep in proposal.dependencies
        )

        # Import operation adapter
        from pydantic import TypeAdapter

        operation_adapter = TypeAdapter(
            type(
                "OperationWrapper",
                (),
                {
                    "kind": str,
                    "path": str,
                    "byte_count": int,
                    "expected_content_hash": str | None,
                    "result_content_hash": str,
                    "patch": str | None,
                    "source_path": str | None,
                    "destination_path": str | None,
                    "quarantine_path": str | None,
                    "source_resource_id": UUID | None,
                },
            )
        )

        operations = tuple(operation_adapter.validate_python(op) for op in proposal.operations)

        change_set = ChangeSet(
            id=uuid4(),
            workspace_id=proposal.workspace_id,
            author_id=proposal.author_id,
            title=proposal.title,
            idempotency_key=proposal.idempotency_key,
            dependencies=dependencies,
            operations=operations,
            risk=proposal.risk,
            status=ChangeSetStatus.PROPOSED,
            created_at=datetime.now(UTC),
        )

        return self._change_set_service.submit(change_set)

    def validate_change_set(self, change_set_id: UUID, validator_id: UUID) -> dict[str, object]:
        """
        Validate a Change Set's dependency closure.
        Returns validation result and transitions to VALIDATED if successful.
        """
        change_set = self._change_set_service.get(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if change_set.status not in (ChangeSetStatus.PROPOSED, ChangeSetStatus.STALE):
            raise ConflictError(
                f"Change Set must be PROPOSED or STALE to validate: {change_set_id}"
            )

        # Perform validation
        validation_result = self._validation_service.validate_dependency_closure(change_set)

        # Record the validation
        self._validation_service.record_validation(change_set_id, validation_result)

        response = {
            "change_set_id": str(change_set_id),
            "is_valid": validation_result.is_valid,
            "validation_result": validation_result.to_dict(),
        }

        # Transition to VALIDATED if validation passed
        if validation_result.is_valid:
            transition = self._change_set_service.transition(
                change_set_id,
                ChangeSetStatus.VALIDATED,
                actor_id=validator_id,
                evidence={"validation_passed_at": datetime.now(UTC).isoformat()},
            )
            response["transition"] = {
                "id": str(transition.id),
                "from_status": transition.from_status.value,
                "to_status": transition.to_status.value,
                "occurred_at": transition.occurred_at.isoformat(),
            }
        else:
            # Mark as STALE if validation failed
            response["validation_failed"] = True
            response["validation_errors"] = validation_result.to_dict()

        return response

    def review_change_set(self, change_set_id: UUID, reviewer_id: UUID) -> dict[str, object]:
        """
        Review a Change Set for authorization.
        Returns review results including authorization evaluation.
        """
        change_set = self._change_set_service.get(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        # Check staleness
        staleness_triggers = self._staleness_service.detect_staleness(change_set)
        relevant_triggers = [t for t in staleness_triggers if t.is_relevant]

        # Evaluate authorization
        auth_result = self._authorization_service.evaluate_authorization(change_set, reviewer_id)

        # Get validation state freshness
        is_fresh = self._validation_service.check_state_freshness(change_set_id)

        return {
            "change_set_id": str(change_set_id),
            "change_set": {
                "id": str(change_set.id),
                "title": change_set.title,
                "status": change_set.status.value,
                "risk": change_set.risk.value,
                "author_id": str(change_set.author_id),
                "created_at": change_set.created_at.isoformat(),
            },
            "staleness_check": {
                "is_stale": len(relevant_triggers) > 0,
                "trigger_count": len(relevant_triggers),
                "triggers": [t.to_dict() for t in relevant_triggers],
            },
            "authorization": auth_result.to_dict(),
            "validation_freshness": {
                "is_fresh": is_fresh,
            },
            "can_proceed": auth_result.is_authorized and len(relevant_triggers) == 0,
        }

    def approve_change_set(
        self,
        change_set_id: UUID,
        approver_id: UUID,
        reason: str | None = None,
        evidence: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """
        Approve a Change Set for execution.
        Transitions to AUTHORIZED status and records immutable decision evidence.
        """
        change_set = self._change_set_service.get(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if change_set.status != ChangeSetStatus.VALIDATED:
            raise ConflictError(f"Change Set must be VALIDATED to approve: {change_set_id}")

        # Revalidate before authorization
        revalidation_result = self._validation_service.revalidate_before_authorization(
            change_set_id, approver_id
        )

        if not revalidation_result.is_valid:
            return {
                "change_set_id": str(change_set_id),
                "approved": False,
                "reason": "Revalidation failed",
                "validation_result": revalidation_result.to_dict(),
            }

        # Evaluate authorization
        auth_result = self._authorization_service.evaluate_authorization(change_set, approver_id)

        if not auth_result.is_authorized:
            return {
                "change_set_id": str(change_set_id),
                "approved": False,
                "reason": "Authorization denied",
                "authorization_result": auth_result.to_dict(),
            }

        # Record the approval decision
        decision = OwnerDecision(
            decision_id=uuid4(),
            change_set_id=change_set_id,
            decision="approved",
            actor_id=approver_id,
            decided_at=datetime.now(UTC),
            reason=reason,
            evidence=evidence,
        )

        self._record_owner_decision(decision)

        # Transition to AUTHORIZED
        transition = self._change_set_service.transition(
            change_set_id,
            ChangeSetStatus.AUTHORIZED,
            actor_id=approver_id,
            evidence={
                "approval_reason": reason or "No reason provided",
                "approval_decision_id": str(decision.decision_id),
            },
        )

        return {
            "change_set_id": str(change_set_id),
            "approved": True,
            "transition": {
                "id": str(transition.id),
                "from_status": transition.from_status.value,
                "to_status": transition.to_status.value,
                "occurred_at": transition.occurred_at.isoformat(),
            },
            "decision": decision.to_dict(),
        }

    def reject_change_set(
        self,
        change_set_id: UUID,
        rejector_id: UUID,
        reason: str | None = None,
        evidence: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """
        Reject a Change Set.
        Transitions to REJECTED status and records immutable decision evidence.
        """
        change_set = self._change_set_service.get(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if change_set.status not in (
            ChangeSetStatus.PROPOSED,
            ChangeSetStatus.VALIDATED,
        ):
            raise ConflictError(
                f"Change Set must be PROPOSED or VALIDATED to reject: {change_set_id}"
            )

        # Record the rejection decision
        decision = OwnerDecision(
            decision_id=uuid4(),
            change_set_id=change_set_id,
            decision="rejected",
            actor_id=rejector_id,
            decided_at=datetime.now(UTC),
            reason=reason,
            evidence=evidence,
        )

        self._record_owner_decision(decision)

        # Transition to REJECTED
        transition = self._change_set_service.transition(
            change_set_id,
            ChangeSetStatus.REJECTED,
            actor_id=rejector_id,
            evidence={
                "rejection_reason": reason or "No reason provided",
                "rejection_decision_id": str(decision.decision_id),
            },
        )

        return {
            "change_set_id": str(change_set_id),
            "rejected": True,
            "transition": {
                "id": str(transition.id),
                "from_status": transition.from_status.value,
                "to_status": transition.to_status.value,
                "occurred_at": transition.occurred_at.isoformat(),
            },
            "decision": decision.to_dict(),
        }

    def supersede_change_set(
        self,
        predecessor_id: UUID,
        successor_proposal: ChangeSetProposal,
        superseder_id: UUID,
        reason: str | None = None,
    ) -> dict[str, object]:
        """
        Supersede an existing Change Set with a new one.
        Creates a successor and links it to the predecessor.
        """
        predecessor = self._change_set_service.get(predecessor_id)
        if predecessor is None:
            raise ConflictError(f"Predecessor Change Set not found: {predecessor_id}")

        if predecessor.successor_id is not None:
            raise ConflictError(f"Predecessor already has a successor: {predecessor_id}")

        # Create the successor Change Set
        successor = self.propose_change_set(successor_proposal)

        # Link predecessor to successor
        updated_successor = self._change_set_service.revise(predecessor_id, successor)

        # Record the supersession decision
        decision = OwnerDecision(
            decision_id=uuid4(),
            change_set_id=predecessor_id,
            decision="superseded",
            actor_id=superseder_id,
            decided_at=datetime.now(UTC),
            reason=reason,
            evidence={"successor_id": str(successor.id)},
        )

        self._record_owner_decision(decision)

        return {
            "predecessor_id": str(predecessor_id),
            "successor_id": str(updated_successor.id),
            "superseded": True,
            "decision": decision.to_dict(),
        }

    def get_change_set(self, change_set_id: UUID) -> dict[str, object]:
        """Get detailed information about a Change Set."""
        change_set = self._change_set_service.get(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        # Get history
        history = self._change_set_service.history(change_set_id)

        # Get staleness triggers
        staleness_triggers = self._staleness_service.get_staleness_triggers(change_set_id)

        # Get owner decisions
        decisions = self._get_owner_decisions(change_set_id)

        return {
            "change_set": {
                "id": str(change_set.id),
                "workspace_id": str(change_set.workspace_id),
                "author_id": str(change_set.author_id),
                "title": change_set.title,
                "idempotency_key": change_set.idempotency_key,
                "risk": change_set.risk.value,
                "status": change_set.status.value,
                "successor_id": str(change_set.successor_id) if change_set.successor_id else None,
                "created_at": change_set.created_at.isoformat(),
            },
            "history": [
                {
                    "id": str(t.id),
                    "from_status": t.from_status.value,
                    "to_status": t.to_status.value,
                    "actor_id": str(t.actor_id) if t.actor_id else None,
                    "occurred_at": t.occurred_at.isoformat(),
                    "evidence": t.evidence,
                }
                for t in history
            ],
            "staleness_triggers": [t.to_dict() for t in staleness_triggers],
            "owner_decisions": [d.to_dict() for d in decisions],
        }

    def _record_owner_decision(self, decision: OwnerDecision) -> None:
        """Record an immutable owner decision."""
        with self._database.connection() as connection:
            # Create table if it doesn't exist (this should be in migrations)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS owner_decisions (
                    decision_id TEXT PRIMARY KEY,
                    change_set_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    reason TEXT,
                    evidence_json TEXT NOT NULL
                )"""
            )

            connection.execute(
                """INSERT INTO owner_decisions VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(decision.decision_id),
                    str(decision.change_set_id),
                    decision.decision,
                    str(decision.actor_id),
                    decision.decided_at.isoformat(),
                    decision.reason,
                    json.dumps(decision.evidence),
                ),
            )

    def _get_owner_decisions(self, change_set_id: UUID) -> tuple[OwnerDecision, ...]:
        """Get all owner decisions for a Change Set."""
        decisions: list[OwnerDecision] = []

        with self._database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM owner_decisions
                   WHERE change_set_id = ?
                   ORDER BY decided_at ASC""",
                (str(change_set_id),),
            ).fetchall()

            for row in rows:
                decision = OwnerDecision(
                    decision_id=UUID(row["decision_id"]),
                    change_set_id=UUID(row["change_set_id"]),
                    decision=row["decision"],
                    actor_id=UUID(row["actor_id"]),
                    decided_at=datetime.fromisoformat(row["decided_at"]),
                    reason=row["reason"],
                    evidence=json.loads(row["evidence_json"]),
                )
                decisions.append(decision)

        return tuple(decisions)
