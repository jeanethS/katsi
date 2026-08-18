"""YOLO Authorization Mode - constrained auto-approval for specific operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.store.workspace_transactions import write_transaction
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    CapabilityOperationClass,
    ChangeSet,
    ChangeSetId,
    Operation,
    RelativePath,
    RiskClass,
    WorkspaceId,
    YoloAuthorization,
    YoloMode,
    YoloModeStatus,
)
from katsi_core.workspace.errors import AuthorizationDeniedError


class YoloSuspensionReason(StrEnum):
    """Standardized reasons for YOLO suspension."""

    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    INVARIANT_FAILURE = "invariant_failure"
    VERIFICATION_FAILURE = "verification_failure"
    RECOVERY_REQUIRED = "recovery_required"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class PolicySimulationResult:
    """Result of policy simulation showing what would be auto-authorized."""

    would_auto_authorize: bool
    matched_policy_rules: tuple[str, ...]
    block_reason: str | None = None


@dataclass(frozen=True, slots=True)
class YoloActivationResult:
    """Result of YOLO mode activation with policy preview."""

    yolo_mode: YoloMode
    policy_simulation: tuple[PolicySimulationResult, ...]


class YoloService:
    """YOLO authorization mode with constrained auto-approval."""

    INITIAL_POLICY_VERSION = "1.0.0"

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def activate(
        self,
        workspace_id: WorkspaceId,
        owner_identity_id: AgentIdentityId,
        agent_identity_id: AgentIdentityId,
        operation_classes: frozenset[CapabilityOperationClass],
        resource_scope: tuple[RelativePath, ...] = (),
        maximum_risk: RiskClass = RiskClass.LOW,
        allow_derived_artifacts: bool = True,
        allow_reversible_organization: bool = True,
        require_owner_approval_for_originals: bool = True,
    ) -> YoloActivationResult:
        """Activate YOLO mode for an agent with policy simulation preview."""
        # Check for existing active mode
        existing = self._get_active_mode(workspace_id, agent_identity_id)
        if existing is not None:
            raise AuthorizationDeniedError(f"Agent already has active YOLO mode: {existing.id}")

        yolo_mode = YoloMode(
            id=uuid4(),
            workspace_id=workspace_id,
            owner_identity_id=owner_identity_id,
            agent_identity_id=agent_identity_id,
            policy_version=self.INITIAL_POLICY_VERSION,
            operation_classes=operation_classes,
            resource_scope=resource_scope,
            maximum_risk=maximum_risk,
            allow_derived_artifacts=allow_derived_artifacts,
            allow_reversible_organization=allow_reversible_organization,
            require_owner_approval_for_originals=require_owner_approval_for_originals,
            status=YoloModeStatus.ACTIVE,
            activated_at=_now(),
            created_at=_now(),
            updated_at=_now(),
        )

        with self._database.connection() as connection, write_transaction(connection):
            connection.execute(
                """INSERT INTO yolo_modes VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    str(yolo_mode.id),
                    str(yolo_mode.workspace_id),
                    str(yolo_mode.owner_identity_id),
                    str(yolo_mode.agent_identity_id),
                    yolo_mode.policy_version,
                    json.dumps(sorted(yolo_mode.operation_classes)),
                    json.dumps(yolo_mode.resource_scope),
                    yolo_mode.maximum_risk.value,
                    int(yolo_mode.allow_derived_artifacts),
                    int(yolo_mode.allow_reversible_organization),
                    int(yolo_mode.require_owner_approval_for_originals),
                    yolo_mode.status.value,
                    yolo_mode.activated_at.isoformat() if yolo_mode.activated_at else None,
                    yolo_mode.suspended_at.isoformat() if yolo_mode.suspended_at else None,
                    yolo_mode.revoked_at.isoformat() if yolo_mode.revoked_at else None,
                    yolo_mode.created_at.isoformat(),
                    yolo_mode.updated_at.isoformat(),
                ),
            )

        # Return with empty policy simulation since no change sets exist yet
        return YoloActivationResult(
            yolo_mode=yolo_mode,
            policy_simulation=(),
        )

    def revoke(
        self,
        workspace_id: WorkspaceId,
        owner_identity_id: AgentIdentityId,
        agent_identity_id: AgentIdentityId,
    ) -> YoloMode:
        """Revoke YOLO mode for an agent."""
        yolo_mode = self._get_active_mode(workspace_id, agent_identity_id)
        if yolo_mode is None:
            raise AuthorizationDeniedError("No active YOLO mode found for agent")

        if yolo_mode.owner_identity_id != owner_identity_id:
            raise AuthorizationDeniedError("Only the owner can revoke YOLO mode")

        with self._database.connection() as connection, write_transaction(connection):
            now = _now().isoformat()
            connection.execute(
                """UPDATE yolo_modes
                   SET status = ?, revoked_at = ?, updated_at = ?
                   WHERE id = ? AND status = ?""",
                (
                    YoloModeStatus.REVOKED.value,
                    now,
                    now,
                    str(yolo_mode.id),
                    YoloModeStatus.ACTIVE.value,
                ),
            )

        updated = self.get(yolo_mode.id)
        assert updated is not None
        return updated

    def suspend(
        self,
        yolo_mode_id: UUID,
        reason: YoloSuspensionReason,
        related_change_set_id: ChangeSetId | None = None,
        related_event_id: UUID | None = None,
    ) -> YoloMode:
        """Automatically suspend YOLO mode due to policy violation."""
        yolo_mode = self.get(yolo_mode_id)
        if yolo_mode is None or yolo_mode.status != YoloModeStatus.ACTIVE:
            raise AuthorizationDeniedError("YOLO mode is not active")

        with self._database.connection() as connection, write_transaction(connection):
            now = _now()
            # Update mode status
            connection.execute(
                """UPDATE yolo_modes
                   SET status = ?, suspended_at = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    YoloModeStatus.SUSPENDED.value,
                    now.isoformat(),
                    now.isoformat(),
                    str(yolo_mode_id),
                ),
            )

            # Record suspension event
            connection.execute(
                """INSERT INTO yolo_suspension_events VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    str(yolo_mode_id),
                    reason.value,
                    str(related_change_set_id) if related_change_set_id else None,
                    str(related_event_id) if related_event_id else None,
                    now.isoformat(),
                ),
            )

        updated = self.get(yolo_mode_id)
        assert updated is not None
        return updated

    def can_auto_authorize(
        self,
        workspace_id: WorkspaceId,
        agent_identity_id: AgentIdentityId,
        change_set: ChangeSet,
    ) -> PolicySimulationResult:
        """Check if a change set can be auto-authorized under YOLO policy."""
        yolo_mode = self._get_active_mode(workspace_id, agent_identity_id)
        if yolo_mode is None:
            return PolicySimulationResult(
                would_auto_authorize=False,
                matched_policy_rules=(),
                block_reason="No active YOLO mode",
            )

        # Check if YOLO mode is active
        if yolo_mode.status != YoloModeStatus.ACTIVE:
            return PolicySimulationResult(
                would_auto_authorize=False,
                matched_policy_rules=(),
                block_reason=f"YOLO mode is {yolo_mode.status.value}",
            )

        # Check operation class scope
        if change_set.risk != RiskClass.LOW and yolo_mode.maximum_risk == RiskClass.LOW:
            return PolicySimulationResult(
                would_auto_authorize=False,
                matched_policy_rules=(),
                block_reason=f"Change set risk {change_set.risk.value} exceeds YOLO maximum {yolo_mode.maximum_risk.value}",
            )

        # Check policy rules for each operation
        matched_rules = []
        for operation in change_set.operations:
            policy_check = self._check_operation_policy(yolo_mode, operation, change_set)
            if not policy_check.allowed:
                return PolicySimulationResult(
                    would_auto_authorize=False,
                    matched_policy_rules=tuple(matched_rules),
                    block_reason=policy_check.reason,
                )
            matched_rules.append(policy_check.rule)

        # Check resource scope
        if yolo_mode.resource_scope:
            for operation in change_set.operations:
                if not self._is_in_scope(operation.path, yolo_mode.resource_scope):
                    return PolicySimulationResult(
                        would_auto_authorize=False,
                        matched_policy_rules=tuple(matched_rules),
                        block_reason=f"Operation path {operation.path} outside YOLO resource scope",
                    )

        return PolicySimulationResult(
            would_auto_authorize=True,
            matched_policy_rules=tuple(matched_rules),
            block_reason=None,
        )

    def record_authorization(
        self,
        yolo_mode_id: UUID,
        change_set_id: ChangeSetId,
        policy_matched: str,
    ) -> YoloAuthorization:
        """Record an auto-authorization event."""
        with self._database.connection() as connection, write_transaction(connection):
            now = _now()
            auth = YoloAuthorization(
                id=uuid4(),
                yolo_mode_id=yolo_mode_id,
                change_set_id=change_set_id,
                auto_authorized=True,
                policy_matched=policy_matched,
                authorized_at=now,
            )
            connection.execute(
                """INSERT INTO yolo_authorizations VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(auth.id),
                    str(yolo_mode_id),
                    str(change_set_id),
                    int(auth.auto_authorized),
                    auth.policy_matched,
                    auth.authorized_at.isoformat(),
                ),
            )
        return auth

    def get_authorization_history(self, yolo_mode_id: UUID) -> tuple[YoloAuthorization, ...]:
        """Get authorization history for a YOLO mode."""
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM yolo_authorizations WHERE yolo_mode_id = ? ORDER BY authorized_at",
                (str(yolo_mode_id),),
            ).fetchall()

        return tuple(
            YoloAuthorization(
                id=UUID(row["id"]),
                yolo_mode_id=UUID(row["yolo_mode_id"]),
                change_set_id=UUID(row["change_set_id"]),
                auto_authorized=bool(row["auto_authorized"]),
                policy_matched=row["policy_matched"],
                authorized_at=datetime.fromisoformat(row["authorized_at"]),
            )
            for row in rows
        )

    def get(self, yolo_mode_id: UUID) -> YoloMode | None:
        """Get YOLO mode by ID."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM yolo_modes WHERE id = ?", (str(yolo_mode_id),)
            ).fetchone()

        if row is None:
            return None

        return self._row_to_yolo_mode(row)

    def get_active_modes(self, workspace_id: WorkspaceId) -> tuple[YoloMode, ...]:
        """Get all active YOLO modes for a workspace."""
        with self._database.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM yolo_modes
                   WHERE workspace_id = ? AND status = ?""",
                (str(workspace_id), YoloModeStatus.ACTIVE.value),
            ).fetchall()

        return tuple(self._row_to_yolo_mode(row) for row in rows)

    def _get_active_mode(
        self, workspace_id: WorkspaceId, agent_identity_id: AgentIdentityId
    ) -> YoloMode | None:
        """Get active YOLO mode for specific agent in workspace."""
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM yolo_modes
                   WHERE workspace_id = ? AND agent_identity_id = ? AND status = ?""",
                (str(workspace_id), str(agent_identity_id), YoloModeStatus.ACTIVE.value),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_yolo_mode(row)

    def _row_to_yolo_mode(self, row: dict) -> YoloMode:
        """Convert database row to YoloMode."""
        return YoloMode(
            id=UUID(row["id"]),
            workspace_id=UUID(row["workspace_id"]),
            owner_identity_id=UUID(row["owner_identity_id"]),
            agent_identity_id=UUID(row["agent_identity_id"]),
            policy_version=row["policy_version"],
            operation_classes=frozenset(
                CapabilityOperationClass(v) for v in json.loads(row["operation_classes_json"])
            ),
            resource_scope=tuple(json.loads(row["resource_scope_json"])),
            maximum_risk=RiskClass(row["maximum_risk"]),
            allow_derived_artifacts=bool(row["allow_derived_artifacts"]),
            allow_reversible_organization=bool(row["allow_reversible_organization"]),
            require_owner_approval_for_originals=bool(row["require_owner_approval_for_originals"]),
            status=YoloModeStatus(row["status"]),
            activated_at=datetime.fromisoformat(row["activated_at"])
            if row["activated_at"]
            else None,
            suspended_at=datetime.fromisoformat(row["suspended_at"])
            if row["suspended_at"]
            else None,
            revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @dataclass(frozen=True, slots=True)
    class _PolicyCheck:
        """Result of policy check for a single operation."""

        allowed: bool
        reason: str | None = None
        rule: str = ""

    def _check_operation_policy(
        self, yolo_mode: YoloMode, operation: Operation, change_set: ChangeSet
    ) -> _PolicyCheck:
        """Check if a single operation complies with YOLO policy."""
        operation_kind = operation.kind

        # Derived artifacts are always allowed if policy permits
        if operation_kind == "replace_derived_artifact":
            if yolo_mode.allow_derived_artifacts:
                return YoloService._PolicyCheck(allowed=True, rule="allow_derived_artifacts")
            return YoloService._PolicyCheck(
                allowed=False,
                reason="Derived artifacts not allowed by YOLO policy",
            )

        # Reversible organization operations
        if operation_kind in ("move_file", "copy_file", "create_directory"):
            if yolo_mode.allow_reversible_organization:
                return YoloService._PolicyCheck(allowed=True, rule="allow_reversible_organization")
            return YoloService._PolicyCheck(
                allowed=False,
                reason="Reversible organization not allowed by YOLO policy",
            )

        # Original modifications require owner approval
        if operation_kind in ("create_file", "replace_file", "apply_patch"):
            if yolo_mode.require_owner_approval_for_originals:
                # Check if this is owner-authored
                if change_set.author_id == yolo_mode.owner_identity_id:
                    return YoloService._PolicyCheck(
                        allowed=False,
                        reason="Owner-authored original modifications require explicit approval",
                    )
                # Non-owner original modifications are blocked
                return YoloService._PolicyCheck(
                    allowed=False,
                    reason="Original modifications not allowed under YOLO policy",
                )
            return YoloService._PolicyCheck(allowed=True, rule="allow_original_modifications")

        # Quarantine and restore operations (reversible)
        if operation_kind in ("quarantine_file", "restore_quarantined_file"):
            if yolo_mode.allow_reversible_organization:
                return YoloService._PolicyCheck(allowed=True, rule="allow_reversible_operations")
            return YoloService._PolicyCheck(
                allowed=False,
                reason="Quarantine operations not allowed by YOLO policy",
            )

        return YoloService._PolicyCheck(
            allowed=False, reason=f"Operation {operation_kind} not covered by YOLO policy"
        )

    def _is_in_scope(self, path: RelativePath, scope: tuple[RelativePath, ...]) -> bool:
        """Check if a path is within the YOLO resource scope."""
        return any(
            path == item or path.startswith(item.rstrip("/") + "/") or item.startswith(path + "/")
            for item in scope
        )
