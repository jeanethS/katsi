"""Tests for Claim authorization requiring proper capability grants.

These tests verify that:
1. Identities without CLAIM capability are denied
2. Expired grants are denied
3. Cross-workspace access is denied
4. Proper error messages are returned
"""

from datetime import UTC, datetime, timedelta
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
    RiskClass,
)
from katsi_core.workspace.errors import AuthorizationDeniedError
from katsi_core.workspace.identity import IdentityService


def test_claim_without_capability_grant_is_denied(tmp_path: Path) -> None:
    """Test that publishing a claim without a capability grant is denied."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should be denied due to missing capability grant",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because agent has no capability grant for CLAIM operations
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "No active capability grant for CLAIM operations" in str(exc_info.value)


def test_claim_with_capability_grant_succeeds(tmp_path: Path) -> None:
    """Test that publishing a claim with proper capability grant succeeds."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create a capability grant for CLAIM operations
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace.id,
        operation_classes=frozenset([CapabilityOperationClass.CLAIM]),
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
                '["claim"]',  # operation_classes_json
                "[]",  # resource_scope_json
                "low",  # maximum_risk
                grant.issued_at.isoformat(),
                None,  # expires_at
                None,  # revoked_at
            ),
        )

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should succeed with proper capability grant",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should succeed because agent has proper capability grant
    published_claim = service.publish(claim)
    assert published_claim.id == claim.id
    assert service.get(claim.id) is not None


def test_expired_capability_grant_is_denied(tmp_path: Path) -> None:
    """Test that expired capability grants are denied."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create an expired capability grant
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace.id,
        operation_classes=frozenset([CapabilityOperationClass.CLAIM]),
        resource_scope=(),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC) - timedelta(days=10),
        expires_at=datetime.now(UTC) - timedelta(days=1),  # Expired yesterday
        revoked_at=None,
    )

    with database.connection() as connection:
        connection.execute(
            """INSERT INTO capability_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(grant.id),
                str(grant.identity_id),
                str(grant.workspace_id),
                '["claim"]',
                "[]",
                "low",
                grant.issued_at.isoformat(),
                grant.expires_at.isoformat(),
                None,
            ),
        )

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should be denied due to expired grant",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because the expired grant is not active
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "No active capability grant for CLAIM operations" in str(exc_info.value)


def test_revoked_capability_grant_is_denied(tmp_path: Path) -> None:
    """Test that revoked capability grants are denied."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create a revoked capability grant
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace.id,
        operation_classes=frozenset([CapabilityOperationClass.CLAIM]),
        resource_scope=(),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC) - timedelta(days=5),
        expires_at=None,
        revoked_at=datetime.now(UTC) - timedelta(days=1),  # Revoked yesterday
    )

    with database.connection() as connection:
        connection.execute(
            """INSERT INTO capability_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(grant.id),
                str(grant.identity_id),
                str(grant.workspace_id),
                '["claim"]',
                "[]",
                "low",
                grant.issued_at.isoformat(),
                None,
                grant.revoked_at.isoformat(),
            ),
        )

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should be denied due to revoked grant",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because the revoked grant is not active
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "No active capability grant for CLAIM operations" in str(exc_info.value)


def test_cross_workspace_access_is_denied(tmp_path: Path) -> None:
    """Test that cross-workspace access is denied."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    (root / "workspace1").mkdir()
    (root / "workspace2").mkdir()
    repository = WorkspaceRepository(database)
    workspace1 = repository.register_workspace(root / "workspace1", "Workspace1")
    workspace2 = repository.register_workspace(root / "workspace2", "Workspace2")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create a capability grant for workspace1 only
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace1.id,  # Grant is for workspace1
        operation_classes=frozenset([CapabilityOperationClass.CLAIM]),
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
                '["claim"]',
                "[]",
                "low",
                grant.issued_at.isoformat(),
                None,
                None,
            ),
        )

    # Try to publish claim in workspace2 (should fail)
    claim = Claim(
        id=uuid4(),
        workspace_id=workspace2.id,  # Different workspace than grant
        author_id=agent.id,
        text="This claim should be denied due to cross-workspace access attempt",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because grant is for workspace1, not workspace2
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "No active capability grant for CLAIM operations" in str(exc_info.value)


def test_inactive_identity_is_denied(tmp_path: Path) -> None:
    """Test that inactive identities are denied even with capability grant."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")

    # Deactivate the identity
    identities.revoke(agent.id)

    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create a capability grant (even though identity is inactive)
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace.id,
        operation_classes=frozenset([CapabilityOperationClass.CLAIM]),
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
                '["claim"]',
                "[]",
                "low",
                grant.issued_at.isoformat(),
                None,
                None,
            ),
        )

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should be denied due to inactive identity",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because identity is inactive/revoked
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "not active" in str(exc_info.value).lower()


def test_wrong_operation_class_in_grant_is_denied(tmp_path: Path) -> None:
    """Test that grants without CLAIM operation class are denied."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)

    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")

    identities = IdentityService(database)
    agent = identities.register("Agent", "test-agent")
    authorization = AuthorizationService(database)
    service = ClaimService(database, identities, authorization)

    # Create a capability grant for CHANGE_SET operations only (not CLAIM)
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=agent.id,
        workspace_id=workspace.id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),  # Wrong operation class
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
                '["change_set"]',  # Wrong operation class
                "[]",
                "low",
                grant.issued_at.isoformat(),
                None,
                None,
            ),
        )

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        text="This claim should be denied due to wrong operation class",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )

    # Should fail because grant doesn't include CLAIM operation class
    with pytest.raises(AuthorizationDeniedError) as exc_info:
        service.publish(claim)

    assert "No active capability grant for CLAIM operations" in str(exc_info.value)
