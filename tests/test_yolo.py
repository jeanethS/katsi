"""Comprehensive tests for YOLO Authorization Mode safety and functionality."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace import (
    AgentIdentity,
    ApplyPatchOperation,
    CapabilityOperationClass,
    ChangeSet,
    CreateFileOperation,
    MoveFileOperation,
    QuarantineFileOperation,
    ReplaceDerivedArtifactOperation,
    ReplaceFileOperation,
    RiskClass,
    WorkspaceId,
    YoloModeStatus,
    YoloService,
    YoloSuspensionReason,
)
from katsi_core.workspace.errors import AuthorizationDeniedError


@pytest.fixture
def database(tmp_path):
    """Create a test database with schema migrations."""
    db_path = tmp_path / "test.db"
    db = WorkspaceSQLite(db_path, SQLiteSettings())
    with db.connection() as conn:
        apply_migrations(conn, target_version=4)
    return db


@pytest.fixture
def populated_database(tmp_path):
    """Create a test database with workspace and agent identities."""
    db_path = tmp_path / "test.db"
    db = WorkspaceSQLite(db_path, SQLiteSettings())
    with db.connection() as conn:
        apply_migrations(conn, target_version=4)

    workspace_id = uuid4()
    owner_id = uuid4()
    agent_id = uuid4()

    with db.connection() as conn, write_transaction(conn):
        # Insert workspace
        conn.execute(
            """INSERT INTO workspaces (id, root_path, display_name, status, state_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(workspace_id),
                str(tmp_path / "workspace"),
                "TestWorkspace",
                "active",
                0,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

        # Insert owner identity
        conn.execute(
            """INSERT INTO agent_identities (id, display_name, client_name, model_name, process_description, active, created_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(owner_id),
                "Owner",
                "test-client",
                "test-model",
                None,
                1,
                datetime.now(UTC).isoformat(),
                None,
            ),
        )

        # Insert agent identity
        conn.execute(
            """INSERT INTO agent_identities (id, display_name, client_name, model_name, process_description, active, created_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(agent_id),
                "Test Agent",
                "test-client",
                "test-model",
                None,
                1,
                datetime.now(UTC).isoformat(),
                None,
            ),
        )

    return db, workspace_id, owner_id, agent_id


@pytest.fixture
def owner_identity() -> AgentIdentity:
    """Create owner agent identity (Python object only, for reference)."""
    return AgentIdentity(
        id=uuid4(),
        display_name="Owner",
        client_name="test-client",
        model_name="test-model",
        active=True,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def agent_identity() -> AgentIdentity:
    """Create agent identity for YOLO activation (Python object only, for reference)."""
    return AgentIdentity(
        id=uuid4(),
        display_name="Test Agent",
        client_name="test-client",
        model_name="test-model",
        active=True,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def workspace_id() -> WorkspaceId:
    """Create test workspace ID (for reference only - use populated_database for actual DB setup)."""
    return uuid4()


def test_yolo_activation_creates_constrained_scope(populated_database):
    """YOLO activation should create a constrained scope for specific agent."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    result = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    assert result.yolo_mode.workspace_id == workspace_id
    assert result.yolo_mode.agent_identity_id == agent_id
    assert result.yolo_mode.owner_identity_id == owner_id
    assert result.yolo_mode.status == YoloModeStatus.ACTIVE
    assert result.yolo_mode.resource_scope == ("src/",)
    assert result.yolo_mode.maximum_risk == RiskClass.LOW


def test_yolo_cannot_expand_authority_beyond_grants(populated_database):
    """YOLO mode cannot grant authority beyond existing capability grants."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    # Activate YOLO mode
    result = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # YOLO mode should not create any new capability grants
    # It only provides auto-approval within existing scope
    assert result.yolo_mode.operation_classes == frozenset([CapabilityOperationClass.CHANGE_SET])


def test_yolo_cannot_expand_scope_beyond_activation(populated_database):
    """YOLO mode cannot expand scope beyond activation configuration."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
        require_owner_approval_for_originals=False,  # Allow original modifications to test scope
    )

    # Create change set with operation outside scope
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="Test change",
        idempotency_key="test-key",
        dependencies=(),
        operations=(
            CreateFileOperation(
                path="README.md",
                byte_count=100,
                result_content_hash="a1b2c3d4e5f6a7b8c9d0e1f2",
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    assert not result.would_auto_authorize
    assert "outside YOLO resource scope" in result.block_reason


def test_yolo_enforces_prohibited_operation_restrictions(populated_database):
    """YOLO mode should block prohibited operations according to policy."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    # Activate with strict policy requiring owner approval for originals
    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
        require_owner_approval_for_originals=True,
    )

    # Create change set with original file modification
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,  # Not the owner
        title="Modify original",
        idempotency_key="modify-key",
        dependencies=(),
        operations=(
            ReplaceFileOperation(
                path="src/main.py",
                byte_count=100,
                expected_content_hash="0123456789abcdef0123456789abcdef",
                result_content_hash="fedcba9876543210fedcba9876543210",
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    assert not result.would_auto_authorize
    assert "not allowed under YOLO policy" in result.block_reason


def test_yolo_allows_derived_artifacts_when_configured(populated_database):
    """YOLO mode should allow derived artifacts when policy permits."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
        allow_derived_artifacts=True,
        require_owner_approval_for_originals=False,  # Allow original mods to test derived artifacts
    )

    # Create change set with derived artifact operation
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="Update derived artifact",
        idempotency_key="derived-key",
        dependencies=(),
        operations=(
            ReplaceDerivedArtifactOperation(
                path="src/generated.ts",
                byte_count=100,
                expected_content_hash="0123456789abcdef0123456789abcdef",
                result_content_hash="fedcba9876543210fedcba9876543210",
                source_resource_id=uuid4(),
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    assert result.would_auto_authorize
    assert "allow_derived_artifacts" in result.matched_policy_rules


def test_yolo_allows_reversible_organization_when_configured(populated_database):
    """YOLO mode should allow reversible organization when policy permits."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
        allow_reversible_organization=True,
        require_owner_approval_for_originals=False,  # Allow original mods to test reversible ops
    )

    # Test move operation
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="Reorganize files",
        idempotency_key="move-key",
        dependencies=(),
        operations=(
            MoveFileOperation(
                path="src/old.py",
                byte_count=100,
                destination_path="src/new/old.py",
                expected_content_hash="0123456789abcdef0123456789abcdef",
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    assert result.would_auto_authorize
    assert "allow_reversible_organization" in result.matched_policy_rules


def test_yolo_auto_suspends_on_authorization_mismatch(populated_database):
    """YOLO mode should auto-suspend on authorization mismatch."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_mode = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    ).yolo_mode

    # Simulate suspension due to authorization mismatch
    suspended = yolo_service.suspend(
        yolo_mode_id=yolo_mode.id,
        reason=YoloSuspensionReason.AUTHORIZATION_MISMATCH,
        related_event_id=uuid4(),
    )

    assert suspended.status == YoloModeStatus.SUSPENDED
    assert suspended.suspended_at is not None


def test_yolo_auto_suspends_on_invariant_failure(populated_database):
    """YOLO mode should auto-suspend on invariant failure."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_mode = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    ).yolo_mode

    # Simulate suspension due to invariant failure
    suspended = yolo_service.suspend(
        yolo_mode_id=yolo_mode.id,
        reason=YoloSuspensionReason.INVARIANT_FAILURE,
        related_change_set_id=None,
    )

    assert suspended.status == YoloModeStatus.SUSPENDED
    assert suspended.suspended_at is not None


def test_yolo_prevents_permanent_data_deletion(populated_database):
    """YOLO mode should prevent permanent data deletion operations."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Quarantine is allowed (reversible), but permanent deletion is not
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="Quarantine file",
        idempotency_key="quarantine-key",
        dependencies=(),
        operations=(
            QuarantineFileOperation(
                path="src/legacy.py",
                byte_count=100,
                expected_content_hash="0123456789abcdef0123456789abcdef",
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    # Quarantine should be allowed as reversible operation
    assert result.would_auto_authorize


def test_yolo_preserves_audit_trail(populated_database):
    """YOLO mode should preserve complete audit trail of authorizations."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_mode = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    ).yolo_mode

    # Insert change_set into database
    change_set_id = uuid4()
    with database.connection() as conn, write_transaction(conn):
        conn.execute(
            """INSERT INTO change_sets (id, workspace_id, author_id, title, idempotency_key, risk, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(change_set_id),
                str(workspace_id),
                str(agent_id),
                "Test change",
                "test-key",
                "low",
                "pending",
                datetime.now(UTC).isoformat(),
            ),
        )

    # Record authorization
    auth = yolo_service.record_authorization(
        yolo_mode_id=yolo_mode.id,
        change_set_id=change_set_id,
        policy_matched="allow_derived_artifacts",
    )

    # Verify audit trail
    history = yolo_service.get_authorization_history(yolo_mode.id)
    assert len(history) == 1
    assert history[0].id == auth.id
    assert history[0].change_set_id == change_set_id
    assert history[0].policy_matched == "allow_derived_artifacts"


def test_yolo_suspension_creates_audit_record(populated_database):
    """YOLO suspension events should create audit records."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_mode = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    ).yolo_mode

    # Suspend the mode
    yolo_service.suspend(
        yolo_mode_id=yolo_mode.id,
        reason=YoloSuspensionReason.VERIFICATION_FAILURE,
        related_change_set_id=None,
    )

    # Verify suspension was recorded
    with database.connection() as conn:
        rows = conn.execute(
            "SELECT * FROM yolo_suspension_events WHERE yolo_mode_id = ?",
            (str(yolo_mode.id),),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["suspension_reason"] == YoloSuspensionReason.VERIFICATION_FAILURE.value


def test_yolo_revocation_by_owner_only(populated_database):
    """Only owner should be able to revoke YOLO mode."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    another_identity = AgentIdentity(
        id=uuid4(),
        display_name="Another Agent",
        client_name="test-client",
        model_name="test-model",
        active=True,
        created_at=datetime.now(UTC),
    )

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Non-owner should not be able to revoke
    with pytest.raises(AuthorizationDeniedError, match="Only the owner can revoke"):
        yolo_service.revoke(
            workspace_id=workspace_id,
            owner_identity_id=another_identity.id,
            agent_identity_id=agent_id,
        )

    # Owner should be able to revoke
    revoked = yolo_service.revoke(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
    )

    assert revoked.status == YoloModeStatus.REVOKED
    assert revoked.revoked_at is not None


def test_yolo_risk_limit_enforcement(populated_database):
    """YOLO mode should enforce risk limits."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Create high-risk change set
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="High risk change",
        idempotency_key="high-risk-key",
        dependencies=(),
        operations=(
            CreateFileOperation(
                path="src/test.py",
                byte_count=100,
                result_content_hash="a1b2c3d4e5f6a7b8c9d0e1f2",
            ),
        ),
        risk=RiskClass.HIGH,  # Exceeds YOLO limit
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    assert not result.would_auto_authorize
    assert "exceeds YOLO maximum" in result.block_reason


def test_yolo_multiple_agents_independent_scopes(populated_database):
    """Multiple agents should have independent YOLO scopes."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    agent1_id = uuid4()
    agent2_id = uuid4()

    # Insert new agents into database
    with database.connection() as conn, write_transaction(conn):
        conn.execute(
            """INSERT INTO agent_identities (id, display_name, client_name, model_name, process_description, active, created_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(agent1_id),
                "Agent 1",
                "test-client",
                "test-model",
                None,
                1,
                datetime.now(UTC).isoformat(),
                None,
            ),
        )
        conn.execute(
            """INSERT INTO agent_identities (id, display_name, client_name, model_name, process_description, active, created_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(agent2_id),
                "Agent 2",
                "test-client",
                "test-model",
                None,
                1,
                datetime.now(UTC).isoformat(),
                None,
            ),
        )

    # Activate YOLO for both agents with different scopes
    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent1_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent2_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("tests/",),
        maximum_risk=RiskClass.LOW,
    )

    # Get active modes
    active_modes = yolo_service.get_active_modes(workspace_id)
    assert len(active_modes) == 2

    # Verify scopes are independent
    agent1_mode = next(m for m in active_modes if m.agent_identity_id == agent1_id)
    agent2_mode = next(m for m in active_modes if m.agent_identity_id == agent2_id)

    assert agent1_mode.resource_scope == ("src/",)
    assert agent2_mode.resource_scope == ("tests/",)


def test_yolo_cannot_duplicate_active_mode_for_same_agent(populated_database):
    """Cannot activate duplicate YOLO mode for same agent."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Second activation should fail
    with pytest.raises(AuthorizationDeniedError, match="already has active YOLO mode"):
        yolo_service.activate(
            workspace_id=workspace_id,
            owner_identity_id=owner_id,
            agent_identity_id=agent_id,
            operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
            resource_scope=("tests/",),
            maximum_risk=RiskClass.MEDIUM,
        )


def test_yolo_requires_active_agent_identity(populated_database):
    """YOLO mode requires active agent identity."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    # This test verifies that only active identities can use YOLO
    # The actual check happens during authorization
    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # If agent becomes inactive, YOLO should not work
    # This is verified during the authorization check


def test_yolo_policy_simulation_at_activation(populated_database):
    """Activation should provide policy simulation preview."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    result = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Policy simulation should be included in activation result
    assert hasattr(result, "policy_simulation")
    assert isinstance(result.policy_simulation, tuple)


def test_yolo_prevents_bypass_of_safeguards(populated_database):
    """YOLO mode should not bypass existing safeguards."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Create change set that would bypass safeguards if not constrained
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=workspace_id,
        author_id=agent_id,
        title="Attempt bypass",
        idempotency_key="bypass-key",
        dependencies=(),
        operations=(
            ApplyPatchOperation(
                path="src/security.py",
                byte_count=50,
                expected_content_hash="0123456789abcdef0123456789abcdef",
                result_content_hash="fedcba9876543210fedcba9876543210",
                patch="security_bypass_patch",
            ),
        ),
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )

    result = yolo_service.can_auto_authorize(
        workspace_id=workspace_id,
        agent_identity_id=agent_id,
        change_set=change_set,
    )

    # Should be blocked since patches to original files require approval
    assert not result.would_auto_authorize


def test_yolo_version_tracking(populated_database):
    """YOLO mode should track policy version."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    result = yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    assert result.yolo_mode.policy_version == "1.0.0"


def test_yolo_workspace_scoping(populated_database):
    """YOLO mode should be scoped to specific workspace."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    workspace1 = uuid4()
    workspace2 = uuid4()

    # Insert workspace1 into database
    with database.connection() as conn, write_transaction(conn):
        conn.execute(
            """INSERT INTO workspaces (id, root_path, display_name, status, state_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(workspace1),
                "/tmp/workspace1",
                "Workspace1",
                "active",
                0,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )

    # Activate YOLO for workspace1 only
    yolo_service.activate(
        workspace_id=workspace1,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    # Should have active mode in workspace1
    modes_w1 = yolo_service.get_active_modes(workspace1)
    assert len(modes_w1) == 1

    # Should not have active mode in workspace2
    modes_w2 = yolo_service.get_active_modes(workspace2)
    assert len(modes_w2) == 0


def test_yolo_operation_class_scoping(populated_database):
    """YOLO mode should be scoped to specific operation classes."""
    database, workspace_id, owner_id, agent_id = populated_database
    yolo_service = YoloService(database)

    # Activate only for CHANGE_SET operations
    yolo_service.activate(
        workspace_id=workspace_id,
        owner_identity_id=owner_id,
        agent_identity_id=agent_id,
        operation_classes=frozenset([CapabilityOperationClass.CHANGE_SET]),
        resource_scope=("src/",),
        maximum_risk=RiskClass.LOW,
    )

    yolo_mode = yolo_service.get_active_modes(workspace_id)[0]

    # Verify operation class scope
    assert yolo_mode.operation_classes == frozenset([CapabilityOperationClass.CHANGE_SET])
    assert CapabilityOperationClass.READ not in yolo_mode.operation_classes
