"""Append-only durable Claim publication, evidence, and status transitions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    ClaimId,
    ClaimStatus,
    ClaimTransition,
)
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

    def __init__(self, database: WorkspaceSQLite, identities: IdentityService) -> None:
        self._database = database
        self._identities = identities

    def publish(self, claim: Claim, evidence: tuple[ClaimEvidence, ...] = ()) -> Claim:
        """Publish a new Claim in proposed state; text and scope never change."""
        self._require_active_identity(claim.author_id)
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
        self._require_active_identity(actor_id)
        if evidence.claim_id != claim_id:
            raise ValueError("Claim evidence must reference its Claim")
        with self._database.connection() as connection, write_transaction(connection):
            if (
                connection.execute("SELECT 1 FROM claims WHERE id = ?", (str(claim_id),)).fetchone()
                is None
            ):
                raise KeyError(f"unknown Claim: {claim_id}")
            self._insert_evidence(connection, (evidence,))

    def transition(
        self,
        claim_id: ClaimId,
        actor_id: AgentIdentityId,
        to_status: ClaimStatus,
        evidence: ClaimEvidence | None = None,
    ) -> ClaimTransition:
        """Append a validated transition; verification needs typed non-agent evidence."""
        self._require_active_identity(actor_id)
        if to_status is ClaimStatus.VERIFIED and (
            evidence is None or evidence.kind not in _VERIFICATION_EVIDENCE
        ):
            raise AuthorizationDeniedError("model or agent evidence cannot verify a Claim")
        timestamp = _now()
        with self._database.connection() as connection, write_transaction(connection):
            row = connection.execute(
                "SELECT status FROM claims WHERE id = ?", (str(claim_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown Claim: {claim_id}")
            from_status = ClaimStatus(row["status"])
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

    def _require_active_identity(self, identity_id: AgentIdentityId) -> None:
        identity = self._identities.get_identity(identity_id)
        if identity is None or not identity.active:
            raise AuthorizationDeniedError("identity is not active")

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
