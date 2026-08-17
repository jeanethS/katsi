"""Authorization evaluation for Change Set operations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import (
    AgentIdentity,
    CapabilityGrant,
    CapabilityOperationClass,
    ChangeSet,
    RiskClass,
)

logger = logging.getLogger(__name__)


class PolicyMode(StrEnum):
    """Authorization policy mode."""

    STRICT = "strict"
    PERMISSIVE = "permissive"
    AUDIT = "audit"


class AuthorizationResult:
    """Result of an authorization evaluation."""

    def __init__(
        self,
        is_authorized: bool,
        denied_reasons: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
        evaluated_at: datetime | None = None,
        policy_mode: PolicyMode = PolicyMode.STRICT,
    ) -> None:
        self.is_authorized = is_authorized
        self.denied_reasons = denied_reasons
        self.warnings = warnings
        self.evaluated_at = evaluated_at or datetime.now(UTC)
        self.policy_mode = policy_mode

    def to_dict(self) -> dict[str, object]:
        """Convert result to serializable dictionary."""
        return {
            "is_authorized": self.is_authorized,
            "denied_reasons": self.denied_reasons,
            "warnings": self.warnings,
            "evaluated_at": self.evaluated_at.isoformat(),
            "policy_mode": self.policy_mode.value,
        }


class AuthorizationService:
    """Evaluates authorization for Change Set operations."""

    def __init__(
        self, database: WorkspaceSQLite, policy_mode: PolicyMode = PolicyMode.STRICT
    ) -> None:
        self._database = database
        self._policy_mode = policy_mode

    def evaluate_authorization(
        self,
        change_set: ChangeSet,
        actor_id: UUID,
        operation_class: CapabilityOperationClass = CapabilityOperationClass.CHANGE_SET,
    ) -> AuthorizationResult:
        """
        Evaluate authorization for a Change Set operation.
        Checks Agent Identity, Capability Grant, active intent, action class,
        scope, limits, and policy mode. Blocks authority-plane operations.
        """
        denied_reasons: list[str] = []
        warnings: list[str] = []

        # 1. Verify Agent Identity
        identity = self._get_agent_identity(actor_id)
        if identity is None:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("Agent identity not found or inactive",),
                policy_mode=self._policy_mode,
            )

        if not identity.active:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("Agent identity is not active",),
                policy_mode=self._policy_mode,
            )

        if identity.revoked_at is not None:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("Agent identity has been revoked",),
                policy_mode=self._policy_mode,
            )

        # 2. Evaluate Capability Grant
        grant = self._get_active_capability_grant(
            actor_id, change_set.workspace_id, operation_class
        )
        if grant is None:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("No active capability grant for this operation class",),
                policy_mode=self._policy_mode,
            )

        # Check grant expiration
        if grant.expires_at and grant.expires_at < datetime.now(UTC):
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("Capability grant has expired",),
                policy_mode=self._policy_mode,
            )

        # Check grant revocation
        if grant.revoked_at is not None:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=("Capability grant has been revoked",),
                policy_mode=self._policy_mode,
            )

        # 3. Verify operation class is in grant
        if operation_class not in grant.operation_classes:
            return AuthorizationResult(
                is_authorized=False,
                denied_reasons=(
                    f"Operation class {operation_class.value} not in capability grant",
                ),
                policy_mode=self._policy_mode,
            )

        # 4. Check active intent compatibility
        intent_compatible = self._check_intent_compatibility(change_set)
        if not intent_compatible:
            denied_reasons.append("Change Set is not compatible with active workspace intent")
            warnings.append("Intent incompatibility detected")

        # 5. Evaluate action class permissions
        action_class_check = self._evaluate_action_class_permissions(
            change_set, grant, operation_class
        )
        if not action_class_check["is_allowed"]:
            denied_reasons.extend(action_class_check["denied_reasons"])

        # 6. Check scope limits
        scope_check = self._check_scope_limits(change_set, grant)
        if not scope_check["within_scope"]:
            denied_reasons.extend(scope_check["violations"])

        # 7. Check risk limits
        if (
            change_set.risk != RiskClass.LOW
            and grant.maximum_risk == RiskClass.LOW
            or (change_set.risk == RiskClass.HIGH and grant.maximum_risk != RiskClass.HIGH)
        ):
            denied_reasons.append(
                f"Change Set risk {change_set.risk.value} exceeds grant maximum {grant.maximum_risk.value}"
            )

        # 8. Block authority-plane operations
        authority_plane_check = self._check_authority_plane_operations(change_set)
        if not authority_plane_check["is_allowed"]:
            denied_reasons.extend(authority_plane_check["denied_reasons"])
            # In strict mode, authority-plane operations are always denied
            if self._policy_mode == PolicyMode.STRICT:
                return AuthorizationResult(
                    is_authorized=False,
                    denied_reasons=tuple(denied_reasons),
                    warnings=tuple(warnings),
                    policy_mode=self._policy_mode,
                )
            else:
                warnings.append("Authority-plane operations require special review")

        # 9. Additional policy mode checks
        if self._policy_mode == PolicyMode.AUDIT:
            warnings.append("Authorization in audit mode - operation will be logged")

        is_authorized = len(denied_reasons) == 0

        return AuthorizationResult(
            is_authorized=is_authorized,
            denied_reasons=tuple(denied_reasons),
            warnings=tuple(warnings),
            policy_mode=self._policy_mode,
        )

    def check_authority_plane_clearance(
        self, actor_id: UUID, workspace_id: UUID
    ) -> AuthorizationResult:
        """
        Check if an actor has authority-plane clearance.
        Authority-plane operations are restricted to authorized owners/admins only.
        """
        # For now, we'll implement a basic check
        # In a real implementation, this would check against an explicit owner/admin role

        # Check if actor is the workspace owner (first agent to register)
        with self._database.connection() as connection:
            owner_check = connection.execute(
                """SELECT ai.*
                   FROM agent_identities ai
                   JOIN workspaces w ON w.created_at >= ai.created_at
                   WHERE ai.id = ? AND ai.client_name = 'owner'
                   LIMIT 1""",
                (str(actor_id),),
            ).fetchone()

            if owner_check:
                return AuthorizationResult(
                    is_authorized=True,
                    policy_mode=self._policy_mode,
                )

        return AuthorizationResult(
            is_authorized=False,
            denied_reasons=("Actor does not have authority-plane clearance",),
            policy_mode=self._policy_mode,
        )

    def _get_agent_identity(self, actor_id: UUID) -> AgentIdentity | None:
        """Retrieve an active Agent Identity by ID."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_identities WHERE id = ?",
                (str(actor_id),),
            ).fetchone()

            if row is None:
                return None

            return AgentIdentity(
                id=UUID(row["id"]),
                display_name=row["display_name"],
                client_name=row["client_name"],
                model_name=row["model_name"],
                process_description=row["process_description"],
                active=bool(row["active"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            )

    def _get_active_capability_grant(
        self,
        actor_id: UUID,
        workspace_id: UUID,
        operation_class: CapabilityOperationClass,
    ) -> CapabilityGrant | None:
        """Retrieve an active Capability Grant for an actor and workspace.

        Selects the most recently issued grant that explicitly includes the
        requested operation class, ignoring grants that do not cover it.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM capability_grants
                   WHERE identity_id = ? AND workspace_id = ? AND revoked_at IS NULL
                   AND (expires_at IS NULL OR expires_at > datetime('now'))
                   AND EXISTS (
                       SELECT 1 FROM json_each(operation_classes_json)
                       WHERE value = ?
                   )
                   ORDER BY issued_at DESC
                   LIMIT 1""",
                (str(actor_id), str(workspace_id), operation_class.value),
            ).fetchone()

            if row is None:
                return None

            import json

            operation_classes = frozenset(
                CapabilityOperationClass(cls) for cls in json.loads(row["operation_classes_json"])
            )
            resource_scope = tuple(json.loads(row["resource_scope_json"]))

            return CapabilityGrant(
                id=UUID(row["id"]),
                identity_id=UUID(row["identity_id"]),
                workspace_id=UUID(row["workspace_id"]),
                operation_classes=operation_classes,
                resource_scope=resource_scope,
                maximum_risk=RiskClass(row["maximum_risk"]),
                issued_at=datetime.fromisoformat(row["issued_at"]),
                expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
                revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            )

    def _check_intent_compatibility(self, change_set: ChangeSet) -> bool:
        """Check if the Change Set is compatible with active workspace intent."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT goal, version FROM workspace_intents WHERE workspace_id = ?",
                (str(change_set.workspace_id),),
            ).fetchone()

            if row is None:
                # No active intent, consider compatible
                return True

            # For now, consider all intents compatible
            # In a real implementation, this would analyze the Change Set
            # against the intent's goals and constraints
            return True

    def _evaluate_action_class_permissions(
        self,
        change_set: ChangeSet,
        grant: CapabilityGrant,
        operation_class: CapabilityOperationClass,
    ) -> dict[str, object]:
        """Evaluate if the action class permissions are satisfied."""
        denied_reasons: list[str] = []

        # Check if the operation class is permitted
        if operation_class not in grant.operation_classes:
            denied_reasons.append(f"Operation class {operation_class.value} not permitted by grant")
            return {
                "is_allowed": False,
                "denied_reasons": tuple(denied_reasons),
            }

        # Check specific operation constraints based on class
        if operation_class == CapabilityOperationClass.CHANGE_SET:
            # CHANGE_SET operations can perform governed file operations
            # No additional restrictions beyond scope and risk
            pass
        elif (
            operation_class == CapabilityOperationClass.GOVERNED_EXECUTION
            and change_set.risk == RiskClass.HIGH
        ):
            # GOVERNED_EXECUTION has stricter requirements for high-risk operations
            denied_reasons.append(
                "High-risk operations require explicit authorization in governed execution mode"
            )

        return {
            "is_allowed": len(denied_reasons) == 0,
            "denied_reasons": tuple(denied_reasons),
        }

    def _check_scope_limits(
        self, change_set: ChangeSet, grant: CapabilityGrant
    ) -> dict[str, object]:
        """Check if the Change Set operations are within grant scope limits."""
        violations: list[str] = []

        # If no scope restrictions, everything is allowed
        if not grant.resource_scope:
            return {
                "within_scope": True,
                "violations": tuple(violations),
            }

        # Check each operation path against the grant scope
        for operation in change_set.operations:
            operation_path = operation.path

            # Check if the operation path is within any granted scope
            within_grant_scope = any(
                self._path_is_within_scope(operation_path, scope_path)
                for scope_path in grant.resource_scope
            )

            if not within_grant_scope:
                violations.append(
                    f"Operation path '{operation_path}' is outside grant scope: {grant.resource_scope}"
                )

        return {
            "within_scope": len(violations) == 0,
            "violations": tuple(violations),
        }

    def _path_is_within_scope(self, operation_path: str, scope_path: str) -> bool:
        """Check if an operation path is within a scope path."""
        # Normalize paths
        operation_path = operation_path.rstrip("/")
        scope_path = scope_path.rstrip("/")

        # Exact match
        if operation_path == scope_path:
            return True

        # Check if operation is within scope (operation_path starts with scope_path/)
        return operation_path.startswith(scope_path + "/")

    def _check_authority_plane_operations(self, change_set: ChangeSet) -> dict[str, object]:
        """Check for authority-plane operations that should be blocked."""
        denied_reasons: list[str] = []

        # Check for operations that modify authorization state
        # These are examples of authority-plane operations
        authority_operation_kinds = {
            "modify_capability_grant",
            "revoke_authorization",
            "alter_workspace_owner",
            "change_authority_plane",
        }

        for operation in change_set.operations:
            # Check if the operation kind is an authority-plane operation
            operation_kind = operation.kind
            if operation_kind in authority_operation_kinds:
                denied_reasons.append(
                    f"Operation '{operation_kind}' is an authority-plane operation"
                )

        # Check for operations that target system/authorization files
        system_paths = {
            ".katsi/auth",
            ".katsi/capabilities",
            ".katsi/permissions",
            ".katsi/owners",
        }

        for operation in change_set.operations:
            operation_path = operation.path
            for system_path in system_paths:
                if operation_path.startswith(system_path):
                    denied_reasons.append(
                        f"Operation targets authority-plane path: {operation_path}"
                    )

        return {
            "is_allowed": len(denied_reasons) == 0,
            "denied_reasons": tuple(denied_reasons),
        }
