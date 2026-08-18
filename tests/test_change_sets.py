from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    CreateFileOperation,
    RiskClass,
)
from katsi_core.workspace.errors import ConflictError, InvalidTransitionError
from katsi_core.workspace.identity import IdentityService

HASH = "a" * 64


def test_submission_is_idempotent_and_transitions_are_append_only(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")
    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Create brief",
        idempotency_key="brief-v1",
        dependencies=(),
        operations=(CreateFileOperation(path="brief.md", byte_count=1, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    assert service.submit(proposal) == proposal
    duplicate = proposal.model_copy(update={"id": uuid4(), "title": "ignored"})
    assert service.submit(duplicate) == proposal

    transition = service.transition(proposal.id, ChangeSetStatus.VALIDATED, author.id)
    assert transition.from_status is ChangeSetStatus.PROPOSED
    assert service.get(proposal.id).status is ChangeSetStatus.VALIDATED  # type: ignore[union-attr]
    assert service.history(proposal.id) == (transition,)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal.id, ChangeSetStatus.VERIFIED, author.id)


def test_immutability_guarantees(tmp_path: Path) -> None:
    """Test that Change Set models are immutable and cannot be modified after creation."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")
    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Immutable proposal",
        idempotency_key="immutable-v1",
        dependencies=(),
        operations=(CreateFileOperation(path="test.md", byte_count=10, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    submitted = service.submit(proposal)

    # Test that the Change Set is frozen/immutable
    with pytest.raises(ValueError):
        submitted.title = "Modified title"

    # Test that operations tuple is immutable
    with pytest.raises(ValueError):
        submitted.operations = ()

    # Test that dependencies tuple is immutable
    with pytest.raises(ValueError):
        submitted.dependencies = ()


def test_successor_relationships(tmp_path: Path) -> None:
    """Test that successor relationships are properly established and immutable."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")

    # Create original Change Set
    original = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Original",
        idempotency_key="original-v1",
        dependencies=(),
        operations=(
            CreateFileOperation(path="original.md", byte_count=5, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    service.submit(original)

    # Create successor
    successor = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Successor",
        idempotency_key="successor-v1",
        dependencies=(),
        operations=(
            CreateFileOperation(path="successor.md", byte_count=10, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service.revise(original.id, successor)

    # Verify successor relationship
    updated_original = service.get(original.id)
    assert updated_original is not None
    assert updated_original.successor_id == successor.id

    # Test that successor cannot have its own successor (single successor chain)
    second_successor = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Second Successor",
        idempotency_key="successor-v2",
        dependencies=(),
        operations=(
            CreateFileOperation(path="second.md", byte_count=15, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ConflictError):
        service.revise(successor.id, second_successor)


def test_idempotency(tmp_path: Path) -> None:
    """Test that idempotency keys prevent duplicate submissions."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")

    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Idempotent proposal",
        idempotency_key="idempotent-key",
        dependencies=(),
        operations=(
            CreateFileOperation(path="idempotent.md", byte_count=20, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    first = service.submit(proposal)

    # Submit with same idempotency key but different content
    duplicate = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Different title",
        idempotency_key="idempotent-key",  # Same key
        dependencies=(),
        operations=(
            CreateFileOperation(path="different.md", byte_count=99, result_content_hash=HASH),
        ),
        risk=RiskClass.HIGH,  # Different risk
        created_at=datetime.now(UTC),
    )
    second = service.submit(duplicate)

    # Should return the original, not the duplicate
    assert first.id == second.id
    assert first.title == "Idempotent proposal"
    assert first.risk == RiskClass.LOW


def test_state_machine_transitions(tmp_path: Path) -> None:
    """Test that all valid state machine transitions work and invalid ones are rejected."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")

    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="State machine test",
        idempotency_key="state-test",
        dependencies=(),
        operations=(CreateFileOperation(path="state.md", byte_count=1, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    service.submit(proposal)

    # Test valid transitions from PROPOSED
    validated = service.transition(proposal.id, ChangeSetStatus.VALIDATED, author.id)
    assert validated.from_status == ChangeSetStatus.PROPOSED
    assert validated.to_status == ChangeSetStatus.VALIDATED

    # Test valid transitions from VALIDATED
    authorized = service.transition(proposal.id, ChangeSetStatus.AUTHORIZED, author.id)
    assert authorized.from_status == ChangeSetStatus.VALIDATED
    assert authorized.to_status == ChangeSetStatus.AUTHORIZED

    # Test valid transitions from AUTHORIZED
    applying = service.transition(proposal.id, ChangeSetStatus.APPLYING, author.id)
    assert applying.from_status == ChangeSetStatus.AUTHORIZED
    assert applying.to_status == ChangeSetStatus.APPLYING

    # Test valid transitions from APPLYING
    applied = service.transition(proposal.id, ChangeSetStatus.APPLIED, author.id)
    assert applied.from_status == ChangeSetStatus.APPLYING
    assert applied.to_status == ChangeSetStatus.APPLIED

    # Test invalid transitions (should raise InvalidTransitionError)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal.id, ChangeSetStatus.PROPOSED, author.id)

    # Test terminal states (cannot transition from)
    service.transition(proposal.id, ChangeSetStatus.VERIFIED, author.id)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal.id, ChangeSetStatus.APPLIED, author.id)


def test_stale_and_rejected_terminal_states(tmp_path: Path) -> None:
    """Test that STALE and REJECTED are terminal states."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")

    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Terminal states test",
        idempotency_key="terminal-test",
        dependencies=(),
        operations=(
            CreateFileOperation(path="terminal.md", byte_count=1, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    service.submit(proposal)

    # Transition to STALE
    service.transition(proposal.id, ChangeSetStatus.STALE, author.id)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal.id, ChangeSetStatus.PROPOSED, author.id)

    # Create another proposal and test REJECTED
    proposal2 = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Rejected proposal",
        idempotency_key="rejected-test",
        dependencies=(),
        operations=(
            CreateFileOperation(path="rejected.md", byte_count=1, result_content_hash=HASH),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service.submit(proposal2)
    service.transition(proposal2.id, ChangeSetStatus.REJECTED, author.id)
    with pytest.raises(InvalidTransitionError):
        service.transition(proposal2.id, ChangeSetStatus.PROPOSED, author.id)


def test_query_apis(tmp_path: Path) -> None:
    """Test the query APIs for validation and authorization evidence."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Workspace")
    author = IdentityService(database).register("Agent", "test")

    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        title="Query APIs test",
        idempotency_key="query-test",
        dependencies=(),
        operations=(CreateFileOperation(path="query.md", byte_count=1, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    service = ChangeSetService(database)
    service.submit(proposal)

    # Test get_with_metadata
    metadata = service.get_with_metadata(proposal.id)
    assert metadata is not None
    assert metadata.operation_count == 1
    assert metadata.total_byte_count == 1
    assert metadata.dependency_count == 0

    # Test validation evidence before validation
    validation_evidence = service.get_validation_evidence(proposal.id)
    assert validation_evidence is None

    # Test authorization evidence before authorization
    auth_evidence = service.get_authorization_evidence(proposal.id)
    assert auth_evidence is None

    # Test terminal action receipt before terminal state
    receipt = service.get_terminal_action_receipt(proposal.id)
    assert receipt is None

    # Create transitions with evidence
    service.transition(
        proposal.id,
        ChangeSetStatus.VALIDATED,
        author.id,
        evidence={"check1": "passed", "check2": "failed", "dependency_satisfied": "true"},
    )
    service.transition(
        proposal.id,
        ChangeSetStatus.AUTHORIZED,
        author.id,
        evidence={"risk_approval": "true", "capability_grant_id": str(uuid4())},
    )

    # Now test the evidence APIs
    validation_evidence = service.get_validation_evidence(proposal.id)
    assert validation_evidence is not None
    assert validation_evidence.change_set_id == proposal.id
    assert validation_evidence.validator_id == author.id
    assert "check1" in validation_evidence.checks_passed
    assert "check2" in validation_evidence.checks_failed
    assert validation_evidence.dependency_satisfied is True

    auth_evidence = service.get_authorization_evidence(proposal.id)
    assert auth_evidence is not None
    assert auth_evidence.change_set_id == proposal.id
    assert auth_evidence.authorizer_id == author.id
    assert auth_evidence.risk_approval is True
