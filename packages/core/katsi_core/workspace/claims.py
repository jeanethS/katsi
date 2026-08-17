"""Append-only durable Claim publication, evidence, and status transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    CapabilityOperationClass,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    ClaimId,
    ClaimStatus,
    ClaimTransition,
)
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.errors import AuthorizationDeniedError, InvalidTransitionError
from katsi_core.workspace.identity import IdentityService


def _now() -> datetime:
    return datetime.now(UTC)


_ALLOWED_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.PROPOSED: frozenset(
        {
            ClaimStatus.CORROBORATED,
            ClaimStatus.VERIFIED,
            ClaimStatus.CONTRADICTED,
            ClaimStatus.SUPERSEDED,
        }
    ),
    ClaimStatus.CORROBORATED: frozenset(
        {ClaimStatus.VERIFIED, ClaimStatus.CONTRADICTED, ClaimStatus.SUPERSEDED}
    ),
    ClaimStatus.VERIFIED: frozenset(
        {ClaimStatus.INVALIDATED, ClaimStatus.CONTRADICTED, ClaimStatus.SUPERSEDED}
    ),
    ClaimStatus.INVALIDATED: frozenset(
        {
            ClaimStatus.CORROBORATED,
            ClaimStatus.VERIFIED,
            ClaimStatus.CONTRADICTED,
            ClaimStatus.SUPERSEDED,
        }
    ),
    ClaimStatus.CONTRADICTED: frozenset({ClaimStatus.SUPERSEDED}),
    ClaimStatus.SUPERSEDED: frozenset(),
}

_VERIFICATION_EVIDENCE = frozenset(
    {ClaimEvidenceKind.DETERMINISTIC, ClaimEvidenceKind.AUTHORITATIVE, ClaimEvidenceKind.OWNER}
)


class ClaimService:
    """Stores immutable assertions and their append-only provenance history."""

    def __init__(
        self, database: WorkspaceSQLite, identities: IdentityService, authorization: AuthorizationService
    ) -> None:
        self._database = database
        self._identities = identities
        self._authorization = authorization

    def publish(self, claim: Claim, evidence: tuple[ClaimEvidence, ...] = ()) -> Claim:
        """Publish a new Claim in proposed state; text and scope never change."""
        self._require_claim_capability(claim.author_id, claim.workspace_id)
        if claim.status is not ClaimStatus.PROPOSED:
            raise InvalidTransitionError("new Claims must start in proposed status")
        if any(item.claim_id != claim.id for item in evidence):
            raise ValueError("Claim evidence must reference the Claim being published")
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(claim.id),
                    str(claim.workspace_id),
                    str(claim.author_id),
                    claim.text,
                    json.dumps(claim.scope_paths),
                    claim.confidence,
                    claim.status.value,
                    claim.created_at.isoformat(),
                ),
            )
            self._insert_evidence(connection, evidence)
        return claim

    def add_evidence(
        self, claim_id: ClaimId, actor_id: AgentIdentityId, evidence: ClaimEvidence
    ) -> None:
        if evidence.claim_id != claim_id:
            raise ValueError("Claim evidence must reference its Claim")
        with self._database.connection() as connection, write_transaction(connection):
            # Check if claim exists and get workspace for authorization
            claim_row = connection.execute(
                "SELECT workspace_id FROM claims WHERE id = ?", (str(claim_id),)
            ).fetchone()
            if claim_row is None:
                raise KeyError(f"unknown Claim: {claim_id}")

            # Perform authorization check with the workspace_id
            workspace_id = UUID(claim_row["workspace_id"])
            self._require_claim_capability(actor_id, workspace_id)

            self._insert_evidence(connection, (evidence,))

    def transition(
        self,
        claim_id: ClaimId,
        actor_id: AgentIdentityId,
        to_status: ClaimStatus,
        evidence: ClaimEvidence | None = None,
    ) -> ClaimTransition:
        """Append a validated transition; verification needs typed non-agent evidence."""
        if to_status is ClaimStatus.VERIFIED and (
            evidence is None or evidence.kind not in _VERIFICATION_EVIDENCE
        ):
            raise AuthorizationDeniedError("model or agent evidence cannot verify a Claim")
        timestamp = _now()
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT workspace_id, status FROM claims WHERE id = ?", (str(claim_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Claim: {claim_id}")
            workspace_id = UUID(row["workspace_id"])
            from_status = ClaimStatus(row["status"])

            # Check authorization after retrieving workspace_id
            self._require_claim_capability(actor_id, workspace_id)

            if to_status not in _ALLOWED_TRANSITIONS[from_status]:
                raise InvalidTransitionError(
                    f"invalid Claim transition: {from_status} -> {to_status}"
                )
            if evidence is not None:
                if evidence.claim_id != claim_id:
                    raise ValueError("Claim evidence must reference its Claim")
                self._insert_evidence(connection, (evidence,))
            transition = ClaimTransition(
                id=uuid4(),
                claim_id=claim_id,
                from_status=from_status,
                to_status=to_status,
                actor_id=actor_id,
                occurred_at=timestamp,
                evidence={"evidence_id": str(evidence.id)} if evidence else {},
            )
            connection.execute(
                "UPDATE claims SET status = ? WHERE id = ?", (to_status.value, str(claim_id))
            )
            connection.execute(
                "INSERT INTO claim_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(transition.id),
                    str(claim_id),
                    from_status.value,
                    to_status.value,
                    str(actor_id),
                    timestamp.isoformat(),
                    json.dumps(transition.evidence),
                ),
            )
        return transition

    def get(self, claim_id: ClaimId) -> Claim | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM claims WHERE id = ?", (str(claim_id),)
            ).fetchone()
        return self._claim_from_row(row) if row else None

    def list_for_workspace(self, workspace_id: UUID) -> list[Claim]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE workspace_id = ? ORDER BY created_at, id",
                (str(workspace_id),),
            ).fetchall()
        return [self._claim_from_row(row) for row in rows]

    def list_claims(self, workspace_id: UUID) -> list[Claim]:
        """List all claims for a workspace (alias for list_for_workspace)."""
        return self.list_for_workspace(workspace_id)

    def transitions(self, claim_id: ClaimId) -> list[ClaimTransition]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM claim_transitions WHERE claim_id = ? ORDER BY occurred_at, id",
                (str(claim_id),),
            ).fetchall()
        return [
            ClaimTransition(
                id=UUID(row["id"]),
                claim_id=claim_id,
                from_status=ClaimStatus(row["from_status"]),
                to_status=ClaimStatus(row["to_status"]),
                actor_id=UUID(row["actor_id"]) if row["actor_id"] else None,
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                evidence=json.loads(row["evidence_json"]),
            )
            for row in rows
        ]

    def invalidate_resource_evidence(
        self, workspace_id: UUID, resource_id: UUID
    ) -> list[ClaimTransition]:
        """Invalidate verified Claims whose evidence names a changed resource.

        The assertion and its earlier verification remain auditable.  No agent
        identity is attributed to this deterministic filesystem consequence.
        """
        timestamp = _now()
        transitions: list[ClaimTransition] = []
        with self._database.connection() as connection, write_transaction(connection):
            evidence_rows = connection.execute(
                """
                SELECT DISTINCT claims.id FROM claims
                JOIN claim_evidence ON claim_evidence.claim_id = claims.id
                WHERE claims.workspace_id = ? AND claims.status = ?
                """,
                (str(workspace_id), ClaimStatus.VERIFIED.value),
            ).fetchall()
            for row in evidence_rows:
                claim_id = UUID(row["id"])
                references = connection.execute(
                    "SELECT reference_json FROM claim_evidence WHERE claim_id = ?",
                    (str(claim_id),),
                ).fetchall()
                if not any(
                    json.loads(reference["reference_json"]).get("resource_id") == str(resource_id)
                    for reference in references
                ):
                    continue
                transition = ClaimTransition(
                    id=uuid4(),
                    claim_id=claim_id,
                    from_status=ClaimStatus.VERIFIED,
                    to_status=ClaimStatus.INVALIDATED,
                    actor_id=None,
                    occurred_at=timestamp,
                    evidence={
                        "reason": "resource_evidence_changed",
                        "resource_id": str(resource_id),
                    },
                )
                connection.execute(
                    "UPDATE claims SET status = ? WHERE id = ?",
                    (ClaimStatus.INVALIDATED.value, str(claim_id)),
                )
                connection.execute(
                    "INSERT INTO claim_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(transition.id),
                        str(claim_id),
                        ClaimStatus.VERIFIED.value,
                        ClaimStatus.INVALIDATED.value,
                        None,
                        timestamp.isoformat(),
                        json.dumps(transition.evidence),
                    ),
                )
                transitions.append(transition)
        return transitions

    def _require_claim_capability(self, actor_id: AgentIdentityId, workspace_id: UUID) -> None:
        """Require that the actor has CLAIM capability for the workspace.

        This replaces the simple identity check with full capability authorization:
        - Identity must exist and be active (not revoked)
        - Must have an active capability grant for CLAIM operations
        - Grant must not be expired or revoked
        - Must be authorized for the specific workspace

        Raises AuthorizationDeniedError with specific error messages for each failure case.
        """
        # Get the agent identity - this checks existence and active/revoked status
        identity = self._identities.get_identity(actor_id)
        if identity is None:
            raise AuthorizationDeniedError("Agent identity not found")
        if not identity.active:
            raise AuthorizationDeniedError("Agent identity is not active")
        if identity.revoked_at is not None:
            raise AuthorizationDeniedError("Agent identity has been revoked")

        # Get active capability grant for CLAIM operations in this workspace
        grant = self._authorization._get_active_capability_grant(
            actor_id, workspace_id, CapabilityOperationClass.CLAIM
        )
        if grant is None:
            raise AuthorizationDeniedError(
                "No active capability grant for CLAIM operations in this workspace"
            )

        # Check grant expiration
        if grant.expires_at and grant.expires_at < datetime.now(UTC):
            raise AuthorizationDeniedError("Capability grant for CLAIM operations has expired")

        # Check grant revocation
        if grant.revoked_at is not None:
            raise AuthorizationDeniedError("Capability grant for CLAIM operations has been revoked")

        # Verify CLAIM operation class is in the grant
        if CapabilityOperationClass.CLAIM not in grant.operation_classes:
            raise AuthorizationDeniedError(
                "CLAIM operation class not included in capability grant"
            )

    @staticmethod
    def _insert_evidence(connection: object, evidence: tuple[ClaimEvidence, ...]) -> None:
        for item in evidence:
            connection.execute(
                "INSERT OR IGNORE INTO claim_evidence VALUES (?, ?, ?, ?, ?)",
                (
                    str(item.id),
                    str(item.claim_id),
                    item.kind.value,
                    json.dumps(item.reference),
                    item.created_at.isoformat(),
                ),
            )

    @staticmethod
    def _claim_from_row(row: object) -> Claim:
        return Claim(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            author_id=UUID(row["author_id"]),
            text=row["text"],
            scope_paths=tuple(json.loads(row["scope_paths_json"])),
            confidence=row["confidence"],
            status=ClaimStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
