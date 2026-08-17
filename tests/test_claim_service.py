import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    CapabilityGrant,
    CapabilityOperationClass,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    ClaimStatus,
    RiskClass,
)
from katsi_core.workspace.errors import AuthorizationDeniedError, InvalidTransitionError
from katsi_core.workspace.identity import IdentityService


def _create_capability_grant(database, identity_id, workspace_id, operation_classes):
    """Helper to create a capability grant for testing."""
    operation_classes = frozenset(operation_classes)
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=identity_id,
        workspace_id=workspace_id,
        operation_classes=operation_classes,
        resource_scope=(),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC),
        expires_at=None,
        revoked_at=None,
    )

    with database.connection() as connection:
        connection.execute(
            """INSERT INTO capability_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(grant.id),
                str(grant.identity_id),
                str(grant.workspace_id),
                json.dumps([cls.value for cls in operation_classes]),
                "[]",  # resource_scope_json
                "low",  # maximum_risk
                grant.issued_at.isoformat(),
                None,  # expires_at
                None,  # revoked_at
            ),
        )


def test_claims_are_immutable_and_only_typed_evidence_can_verify(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    author = identities.register("Agent", "test")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create capability grant for CLAIM operations
    _create_capability_grant(database, author.id, workspace.id, [CapabilityOperationClass.CLAIM])

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="The parser supports Markdown.",
        scope_paths=("docs/readme.md",),
        confidence=0.99,
        created_at=datetime.now(UTC),
    )
    agent_evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=claim.id,
        kind=ClaimEvidenceKind.AGENT,
        reference={"note": "observed in source"},
        created_at=datetime.now(UTC),
    )
    service.publish(claim, (agent_evidence,))
    assert service.get(claim.id) == claim

    with pytest.raises(AuthorizationDeniedError):
        service.transition(claim.id, author.id, ClaimStatus.VERIFIED, agent_evidence)
    corroborated = service.transition(claim.id, author.id, ClaimStatus.CORROBORATED)
    assert corroborated.from_status is ClaimStatus.PROPOSED
    deterministic = ClaimEvidence(
        id=uuid4(),
        claim_id=claim.id,
        kind=ClaimEvidenceKind.DETERMINISTIC,
        reference={"verifier": "unit-test", "result": "pass"},
        created_at=datetime.now(UTC),
    )
    verified = service.transition(claim.id, author.id, ClaimStatus.VERIFIED, deterministic)
    assert verified.to_status is ClaimStatus.VERIFIED
    assert service.get(claim.id).status is ClaimStatus.VERIFIED  # type: ignore[union-attr]
    assert [item.to_status for item in service.transitions(claim.id)] == [
        ClaimStatus.CORROBORATED,
        ClaimStatus.VERIFIED,
    ]
    with pytest.raises(InvalidTransitionError):
        service.transition(claim.id, author.id, ClaimStatus.PROPOSED)


def test_revoked_author_cannot_transition_existing_claim(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    author = identities.register("Agent", "test")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create capability grant for CLAIM operations
    _create_capability_grant(database, author.id, workspace.id, [CapabilityOperationClass.CLAIM])

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="A durable assertion.",
        confidence=0.5,
        created_at=datetime.now(UTC),
    )
    service.publish(claim)
    identities.revoke(author.id)
    with pytest.raises(AuthorizationDeniedError):
        service.transition(claim.id, author.id, ClaimStatus.CONTRADICTED)
    assert service.get(claim.id).author_id == author.id  # type: ignore[union-attr]


def test_changed_resource_evidence_invalidates_only_dependent_verified_claims(
    tmp_path: Path,
) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 2)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    author = identities.register("Agent", "test")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create capability grant for CLAIM operations
    _create_capability_grant(database, author.id, workspace.id, [CapabilityOperationClass.CLAIM])

    dependent_resource, independent_resource = uuid4(), uuid4()
    claims = []
    for resource_id in (dependent_resource, independent_resource):
        claim = Claim(
            id=uuid4(),
            workspace_id=workspace.id,
            author_id=author.id,
            text=f"Resource {resource_id} is valid.",
            confidence=0.8,
            created_at=datetime.now(UTC),
        )
        evidence = ClaimEvidence(
            id=uuid4(),
            claim_id=claim.id,
            kind=ClaimEvidenceKind.RESOURCE_VERSION,
            reference={"resource_id": str(resource_id), "version_id": str(uuid4())},
            created_at=datetime.now(UTC),
        )
        service.publish(claim, (evidence,))
        verification = ClaimEvidence(
            id=uuid4(),
            claim_id=claim.id,
            kind=ClaimEvidenceKind.DETERMINISTIC,
            reference={"resource_id": str(resource_id), "verifier": "unit-test"},
            created_at=datetime.now(UTC),
        )
        service.transition(claim.id, author.id, ClaimStatus.VERIFIED, verification)
        claims.append(claim)

    invalidated = service.invalidate_resource_evidence(workspace.id, dependent_resource)
    assert [transition.claim_id for transition in invalidated] == [claims[0].id]
    assert service.get(claims[0].id).status is ClaimStatus.INVALIDATED  # type: ignore[union-attr]
    assert service.get(claims[1].id).status is ClaimStatus.VERIFIED  # type: ignore[union-attr]


def test_competing_claims_keep_history_without_storing_transcripts(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 2)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    first = identities.register("First", "test")
    second = identities.register("Second", "test")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create capability grants for CLAIM operations for both agents
    _create_capability_grant(database, first.id, workspace.id, [CapabilityOperationClass.CLAIM])
    _create_capability_grant(database, second.id, workspace.id, [CapabilityOperationClass.CLAIM])
    first_claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=first.id,
        text="The selected backend is SQLite.",
        confidence=0.7,
        created_at=datetime.now(UTC),
    )
    competing_claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=second.id,
        text="The selected backend is an append-only JSON log.",
        confidence=0.7,
        created_at=datetime.now(UTC),
    )
    service.publish(first_claim)
    service.publish(competing_claim)
    service.transition(first_claim.id, first.id, ClaimStatus.CONTRADICTED)
    service.transition(competing_claim.id, second.id, ClaimStatus.SUPERSEDED)
    assert {claim.status for claim in service.list_for_workspace(workspace.id)} == {
        ClaimStatus.CONTRADICTED,
        ClaimStatus.SUPERSEDED,
    }
    assert [transition.to_status for transition in service.transitions(first_claim.id)] == [
        ClaimStatus.CONTRADICTED
    ]
    with database.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(claims)").fetchall()}
    assert "transcript" not in columns
    assert "reasoning" not in columns


def test_recent_non_claim_grant_does_not_shadow_claim_grant(tmp_path: Path) -> None:
    """A newer grant without CLAIM must not hide an older active CLAIM grant."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    identities = IdentityService(database)
    author = identities.register("Agent", "test")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Older CLAIM grant
    _create_capability_grant(database, author.id, workspace.id, [CapabilityOperationClass.CLAIM])
    # Newer READ-only grant
    _create_capability_grant(database, author.id, workspace.id, [CapabilityOperationClass.READ])

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Authorization should still succeed.",
        confidence=0.8,
        created_at=datetime.now(UTC),
    )
    service.publish(claim)

    assert service.get(claim.id) == claim
