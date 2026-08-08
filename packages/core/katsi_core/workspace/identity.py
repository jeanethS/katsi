"""Durable local agent identity, credential, and capability services."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentity,
    AgentIdentityId,
    CapabilityGrant,
    CapabilityOperationClass,
    RiskClass,
    WorkspaceId,
)
from katsi_core.workspace.errors import AuthorizationDeniedError


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    identity: AgentIdentity
    credential: str


class IdentityService:
    """Owner-only identity administration and capability evaluation."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def register(
        self, display_name: str, client_name: str, model_name: str | None = None
    ) -> AgentIdentity:
        identity = AgentIdentity(
            id=uuid4(),
            display_name=display_name,
            client_name=client_name,
            model_name=model_name,
            created_at=_now(),
        )
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO agent_identities VALUES (?, ?, ?, ?, ?, 1, ?, NULL)",
                (
                    str(identity.id),
                    identity.display_name,
                    identity.client_name,
                    identity.model_name,
                    identity.process_description,
                    identity.created_at.isoformat(),
                ),
            )
        return identity

    def issue_credential(self, identity_id: AgentIdentityId) -> IssuedCredential:
        identity = self.get_identity(identity_id)
        if identity is None or not identity.active:
            raise AuthorizationDeniedError("identity is not active")
        credential = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        digest = hashlib.sha256(salt + credential.encode()).hexdigest()
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO agent_credentials VALUES (?, ?, ?, ?, ?, NULL)",
                (str(uuid4()), str(identity_id), digest, salt.hex(), _now().isoformat()),
            )
        return IssuedCredential(identity, credential)

    def rotate_credential(self, identity_id: AgentIdentityId) -> IssuedCredential:
        """Invalidate prior credentials before returning the sole new secret."""
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = ? WHERE identity_id = ? AND revoked_at IS NULL",
                (_now().isoformat(), str(identity_id)),
            )
        return self.issue_credential(identity_id)

    def authenticate(self, credential: str) -> AgentIdentity:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_credentials WHERE revoked_at IS NULL"
            ).fetchall()
        for row in rows:
            digest = hashlib.sha256(bytes.fromhex(row["salt"]) + credential.encode()).hexdigest()
            if hmac.compare_digest(digest, row["credential_hash"]):
                identity = self.get_identity(UUID(row["identity_id"]))
                if identity is not None and identity.active:
                    return identity
        raise AuthorizationDeniedError("invalid or revoked credential")

    def revoke(self, identity_id: AgentIdentityId) -> None:
        with self._database.connection() as connection, write_transaction(connection):
            timestamp = _now().isoformat()
            connection.execute(
                "UPDATE agent_identities SET active = 0, revoked_at = ? WHERE id = ?",
                (timestamp, str(identity_id)),
            )
            connection.execute(
                "UPDATE agent_credentials SET revoked_at = ? WHERE identity_id = ? AND revoked_at IS NULL",
                (timestamp, str(identity_id)),
            )

    def grant(self, grant: CapabilityGrant) -> None:
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "INSERT INTO capability_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(grant.id),
                    str(grant.identity_id),
                    str(grant.workspace_id),
                    json.dumps(sorted(grant.operation_classes)),
                    json.dumps(grant.resource_scope),
                    grant.maximum_risk.value,
                    grant.issued_at.isoformat(),
                    grant.expires_at.isoformat() if grant.expires_at else None,
                    grant.revoked_at.isoformat() if grant.revoked_at else None,
                ),
            )

    def revoke_grant(self, grant_id: UUID) -> None:
        """Immediately remove a capability without erasing its audit record."""
        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                "UPDATE capability_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now().isoformat(), str(grant_id)),
            )

    def authorize(
        self,
        identity_id: AgentIdentityId,
        workspace_id: WorkspaceId,
        operation: CapabilityOperationClass,
        path: str | None,
        risk: RiskClass,
    ) -> CapabilityGrant:
        identity = self.get_identity(identity_id)
        if identity is None or not identity.active:
            raise AuthorizationDeniedError("identity is not active")
        now = _now()
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM capability_grants WHERE identity_id = ? AND workspace_id = ? AND revoked_at IS NULL",
                (str(identity_id), str(workspace_id)),
            ).fetchall()
        order = {RiskClass.LOW: 0, RiskClass.MEDIUM: 1, RiskClass.HIGH: 2}
        for row in rows:
            expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
            classes = frozenset(
                CapabilityOperationClass(value)
                for value in json.loads(row["operation_classes_json"])
            )
            scope = tuple(json.loads(row["resource_scope_json"]))
            if (
                operation in classes
                and (expires is None or expires > now)
                and order[risk] <= order[RiskClass(row["maximum_risk"])]
                and (
                    path is None
                    or not scope
                    or any(path == item or path.startswith(f"{item}/") for item in scope)
                )
            ):
                return CapabilityGrant(
                    id=UUID(row["id"]),
                    identity_id=identity_id,
                    workspace_id=workspace_id,
                    operation_classes=classes,
                    resource_scope=scope,
                    maximum_risk=RiskClass(row["maximum_risk"]),
                    issued_at=datetime.fromisoformat(row["issued_at"]),
                    expires_at=expires,
                )
        raise AuthorizationDeniedError("no active capability grant covers this operation")

    def get_identity(self, identity_id: AgentIdentityId) -> AgentIdentity | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_identities WHERE id = ?", (str(identity_id),)
            ).fetchone()
        if row is None:
            return None
        revoked = datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None
        return AgentIdentity(
            id=UUID(row["id"]),
            display_name=row["display_name"],
            client_name=row["client_name"],
            model_name=row["model_name"],
            process_description=row["process_description"],
            active=bool(row["active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=revoked,
        )
