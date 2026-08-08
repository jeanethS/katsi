from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import CapabilityGrant, CapabilityOperationClass, RiskClass
from katsi_core.workspace.errors import AuthorizationDeniedError
from katsi_core.workspace.identity import IdentityService


def test_credentials_revocation_and_scoped_capabilities(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    service = IdentityService(database)
    identity = service.register("Agent", "test-client", "local-model")
    issued = service.issue_credential(identity.id)
    assert service.authenticate(issued.credential).id == identity.id
    with database.connection() as connection:
        assert issued.credential not in str(
            connection.execute("SELECT * FROM agent_credentials").fetchone()
        )
    rotated = service.rotate_credential(identity.id)
    with pytest.raises(AuthorizationDeniedError):
        service.authenticate(issued.credential)
    assert service.authenticate(rotated.credential).id == identity.id

    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=identity.id,
        workspace_id=workspace.id,
        operation_classes=frozenset({CapabilityOperationClass.CLAIM}),
        resource_scope=("docs",),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC),
    )
    service.grant(grant)
    assert (
        service.authorize(
            identity.id, workspace.id, CapabilityOperationClass.CLAIM, "docs/a.md", RiskClass.LOW
        ).id
        == grant.id
    )
    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            identity.id, workspace.id, CapabilityOperationClass.CLAIM, "src/a.py", RiskClass.LOW
        )
    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            identity.id, workspace.id, CapabilityOperationClass.CLAIM, "docs/a.md", RiskClass.HIGH
        )
    service.revoke_grant(grant.id)
    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            identity.id, workspace.id, CapabilityOperationClass.CLAIM, "docs/a.md", RiskClass.LOW
        )
    service.revoke(identity.id)
    with pytest.raises(AuthorizationDeniedError):
        service.authenticate(issued.credential)


def test_expired_and_cross_workspace_grants_are_denied(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    repository = WorkspaceRepository(database)
    first, second = (
        repository.register_workspace(one, "One"),
        repository.register_workspace(two, "Two"),
    )
    service = IdentityService(database)
    identity = service.register("Agent", "test")
    service.grant(
        CapabilityGrant(
            id=uuid4(),
            identity_id=identity.id,
            workspace_id=first.id,
            operation_classes=frozenset({CapabilityOperationClass.READ}),
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            identity.id, second.id, CapabilityOperationClass.READ, None, RiskClass.LOW
        )


def test_identity_labels_do_not_confer_authority_and_revocation_denies_grants(
    tmp_path: Path,
) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    root = tmp_path / "project"
    root.mkdir()
    workspace = WorkspaceRepository(database).register_workspace(root, "Project")
    service = IdentityService(database)
    authorized = service.register("Agent", "trusted-client")
    forged_label = service.register("Agent", "untrusted-client")
    grant = CapabilityGrant(
        id=uuid4(),
        identity_id=authorized.id,
        workspace_id=workspace.id,
        operation_classes=frozenset({CapabilityOperationClass.READ}),
        maximum_risk=RiskClass.LOW,
        issued_at=datetime.now(UTC),
    )
    service.grant(grant)

    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            forged_label.id,
            workspace.id,
            CapabilityOperationClass.READ,
            None,
            RiskClass.LOW,
        )

    service.revoke(authorized.id)
    with pytest.raises(AuthorizationDeniedError):
        service.authorize(
            authorized.id,
            workspace.id,
            CapabilityOperationClass.READ,
            None,
            RiskClass.LOW,
        )
