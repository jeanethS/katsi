"""Dependency-closure validation and revalidation for Change Sets."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import (
    ActionOutcomeStatus,
    ChangeSet,
    ChangeSetStatus,
    Operation,
    ResourceDependency,
    ResourceStatus,
    RiskClass,
    WorkspaceEvent,
    WorkspaceEventKind,
)
from katsi_core.workspace.errors import ConflictError, StaleStateError

logger = logging.getLogger(__name__)


class ValidationResult:
    """Result of validating a Change Set's dependency closure."""

    def __init__(
        self,
        is_valid: bool,
        violated_dependencies: tuple[ResourceDependency, ...] = (),
        missing_resources: tuple[UUID, ...] = (),
        hash_mismatches: tuple[tuple[UUID, str, str], ...] = (),  # (resource_id, expected, actual)
        unexpected_presence: tuple[UUID, ...] = (),
        invariant_violations: tuple[str, ...] = (),
        intended_output_mismatches: tuple[str, ...] = (),
        validated_at: datetime | None = None,
    ) -> None:
        self.is_valid = is_valid
        self.violated_dependencies = violated_dependencies
        self.missing_resources = missing_resources
        self.hash_mismatches = hash_mismatches
        self.unexpected_presence = unexpected_presence
        self.invariant_violations = invariant_violations
        self.intended_output_mismatches = intended_output_mismatches
        self.validated_at = validated_at or datetime.now(UTC)

    def to_dict(self) -> dict[str, object]:
        """Convert validation result to serializable dictionary."""
        return {
            "is_valid": self.is_valid,
            "violated_dependencies": [
                {
                    "resource_id": str(d.resource_id),
                    "expected_version_id": str(d.expected_version_id) if d.expected_version_id else None,
                    "expected_content_hash": d.expected_content_hash,
                    "expected_absent": d.expected_absent,
                }
                for d in self.violated_dependencies
            ],
            "missing_resources": [str(r) for r in self.missing_resources],
            "hash_mismatches": [
                {"resource_id": str(r), "expected": e, "actual": a}
                for r, e, a in self.hash_mismatches
            ],
            "unexpected_presence": [str(r) for r in self.unexpected_presence],
            "invariant_violations": self.invariant_violations,
            "intended_output_mismatches": self.intended_output_mismatches,
            "validated_at": self.validated_at.isoformat(),
        }


class ValidationService:
    """Validates Change Set dependency closure and revalidates before authorization."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def validate_dependency_closure(self, change_set: ChangeSet) -> ValidationResult:
        """
        Validate dependency closure against exact resource versions, target hashes,
        absence assertions, invariants, and intended outputs.
        """
        violated_dependencies: list[ResourceDependency] = []
        missing_resources: list[UUID] = []
        hash_mismatches: list[tuple[UUID, str, str]] = []
        unexpected_presence: list[UUID] = []
        invariant_violations: list[str] = []
        intended_output_mismatches: list[str] = []

        with self._database.connection() as connection:
            for dependency in change_set.dependencies:
                resource_row = connection.execute(
                    "SELECT * FROM resources WHERE id = ?",
                    (str(dependency.resource_id),),
                ).fetchone()

                if resource_row is None:
                    if dependency.expected_absent:
                        # Resource is absent as expected
                        continue
                    else:
                        missing_resources.append(dependency.resource_id)
                        violated_dependencies.append(dependency)
                    continue

                # Check if resource is deleted but presence was expected
                if resource_row["status"] == ResourceStatus.DELETED.value:
                    if not dependency.expected_absent:
                        violated_dependencies.append(dependency)
                        missing_resources.append(dependency.resource_id)
                    continue

                # Validate expected content hash
                if dependency.expected_content_hash:
                    latest_version = connection.execute(
                        """SELECT rv.* FROM resource_versions rv
                           INNER JOIN resources r ON rv.resource_id = r.id
                           WHERE r.id = ?
                           ORDER BY rv.observed_at DESC
                           LIMIT 1""",
                        (str(dependency.resource_id),),
                    ).fetchone()

                    if latest_version is None:
                        missing_resources.append(dependency.resource_id)
                        violated_dependencies.append(dependency)
                    elif latest_version["content_hash"] != dependency.expected_content_hash:
                        hash_mismatches.append(
                            (
                                dependency.resource_id,
                                dependency.expected_content_hash,
                                latest_version["content_hash"],
                            )
                        )
                        violated_dependencies.append(dependency)

                # Validate expected version ID
                if dependency.expected_version_id:
                    version_exists = connection.execute(
                        "SELECT 1 FROM resource_versions WHERE id = ? AND resource_id = ?",
                        (str(dependency.expected_version_id), str(dependency.resource_id)),
                    ).fetchone()

                    if not version_exists:
                        missing_resources.append(dependency.resource_id)
                        violated_dependencies.append(dependency)

                # Check for unexpected presence
                if dependency.expected_absent and resource_row["status"] != ResourceStatus.DELETED.value:
                    unexpected_presence.append(dependency.resource_id)
                    violated_dependencies.append(dependency)

            # Validate invariants if defined
            invariants = self._get_workspace_invariants(connection, change_set.workspace_id)
            for invariant in invariants:
                violation = self._check_invariant(
                    connection, change_set.workspace_id, invariant, change_set
                )
                if violation:
                    invariant_violations.append(violation)

            # Validate intended outputs
            intended_outputs = self._get_intended_outputs(connection, change_set.workspace_id)
            for output_check in intended_outputs:
                mismatch = self._check_intended_output(
                    connection, change_set.workspace_id, output_check, change_set
                )
                if mismatch:
                    intended_output_mismatches.append(mismatch)

        is_valid = (
            not violated_dependencies
            and not missing_resources
            and not hash_mismatches
            and not unexpected_presence
            and not invariant_violations
            and not intended_output_mismatches
        )

        return ValidationResult(
            is_valid=is_valid,
            violated_dependencies=tuple(violated_dependencies),
            missing_resources=tuple(missing_resources),
            hash_mismatches=tuple(hash_mismatches),
            unexpected_presence=tuple(unexpected_presence),
            invariant_violations=tuple(invariant_violations),
            intended_output_mismatches=tuple(intended_output_mismatches),
        )

    def revalidate_before_authorization(
        self, change_set_id: UUID, actor_id: UUID
    ) -> ValidationResult:
        """
        Revalidate a Change Set immediately before authorization.
        Ensures state hasn't changed since initial validation.
        """
        change_set = self._get_change_set(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if change_set.status not in (ChangeSetStatus.VALIDATED, ChangeSetStatus.STALE):
            raise ConflictError(
                f"Change Set must be VALIDATED or STALE before authorization: {change_set_id}"
            )

        # Get last validation result to compare
        last_validation = self._get_last_validation_result(change_set_id)
        current_validation = self.validate_dependency_closure(change_set)

        # Check if state changed since last validation
        if last_validation:
            state_changed = self._did_validation_state_change(last_validation, current_validation)
            if state_changed:
                logger.warning(
                    f"State changed since last validation for Change Set {change_set_id}"
                )
                return current_validation

        return current_validation

    def revalidate_before_replacement(
        self, change_set_id: UUID, operation_index: int
    ) -> ValidationResult:
        """
        Revalidate immediately before each target replacement.
        Ensures specific operation target is still valid.
        """
        change_set = self._get_change_set(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if operation_index >= len(change_set.operations):
            raise ConflictError(f"Invalid operation index: {operation_index}")

        operation = change_set.operations[operation_index]
        operation_validation = self._validate_single_operation(change_set, operation)

        if not operation_validation.is_valid:
            raise StaleStateError(
                f"Operation {operation_index} target is no longer valid: {operation_validation}"
            )

        return operation_validation

    def check_state_freshness(
        self, change_set_id: UUID, max_age_seconds: int = 300
    ) -> bool:
        """
        Check if validation state is still fresh.
        Returns True if validation is within max_age_seconds.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT validated_at FROM change_set_validations
                   WHERE change_set_id = ?
                   ORDER BY validated_at DESC
                   LIMIT 1""",
                (str(change_set_id),),
            ).fetchone()

            if row is None:
                return False

            validated_at = datetime.fromisoformat(row["validated_at"])
            age = (datetime.now(UTC) - validated_at).total_seconds()

            return age <= max_age_seconds

    def _get_change_set(self, change_set_id: UUID) -> ChangeSet | None:
        """Retrieve a Change Set by ID."""
        from katsi_core.workspace.change_sets import ChangeSetService

        return ChangeSetService(self._database).get(change_set_id)

    def _get_last_validation_result(
        self, change_set_id: UUID
    ) -> dict[str, object] | None:
        """Get the last validation result for comparison."""
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT validation_result_json FROM change_set_validations
                   WHERE change_set_id = ?
                   ORDER BY validated_at DESC
                   LIMIT 1""",
                (str(change_set_id),),
            ).fetchone()

            if row is None:
                return None

            return json.loads(row["validation_result_json"])

    def _did_validation_state_change(
        self, old_result: dict[str, object], new_result: ValidationResult
    ) -> bool:
        """Check if validation state changed since last validation."""
        old_is_valid = old_result.get("is_valid", False)
        if old_is_valid != new_result.is_valid:
            return True

        old_violations = set(
            dep["resource_id"]
            for dep in old_result.get("violated_dependencies", [])
        )
        new_violations = {str(d.resource_id) for d in new_result.violated_dependencies}

        return old_violations != new_violations

    def _validate_single_operation(
        self, change_set: ChangeSet, operation: Operation
    ) -> ValidationResult:
        """Validate a single operation's target state."""
        # Extract relevant dependencies for this operation
        operation_path = operation.path

        relevant_deps = [
            dep
            for dep in change_set.dependencies
            if self._is_dependency_relevant_to_operation(dep, operation_path)
        ]

        # Create a focused validation for just this operation
        with self._database.connection() as connection:
            for dep in relevant_deps:
                resource_row = connection.execute(
                    "SELECT * FROM resources WHERE id = ?",
                    (str(dep.resource_id),),
                ).fetchone()

                if dep.expected_absent:
                    if resource_row and resource_row["status"] != ResourceStatus.DELETED.value:
                        return ValidationResult(
                            is_valid=False,
                            violated_dependencies=(dep,),
                            unexpected_presence=(dep.resource_id,),
                        )
                elif dep.expected_content_hash:
                    latest_version = connection.execute(
                        """SELECT rv.* FROM resource_versions rv
                           INNER JOIN resources r ON rv.resource_id = r.id
                           WHERE r.id = ?
                           ORDER BY rv.observed_at DESC
                           LIMIT 1""",
                        (str(dep.resource_id),),
                    ).fetchone()

                    if (
                        latest_version is None
                        or latest_version["content_hash"] != dep.expected_content_hash
                    ):
                        return ValidationResult(
                            is_valid=False,
                            violated_dependencies=(dep,),
                            hash_mismatches=(
                                (
                                    dep.resource_id,
                                    dep.expected_content_hash,
                                    latest_version["content_hash"] if latest_version else "missing",
                                ),
                            ),
                        )

        return ValidationResult(is_valid=True)

    def _is_dependency_relevant_to_operation(
        self, dependency: ResourceDependency, operation_path: str
    ) -> bool:
        """Check if a dependency is relevant to a specific operation."""
        # For now, consider all dependencies relevant
        # In a more sophisticated implementation, we would check path relationships
        return True

    def _get_workspace_invariants(
        self, connection, workspace_id: UUID
    ) -> tuple[str, ...]:
        """Retrieve invariant definitions for the workspace."""
        row = connection.execute(
            "SELECT invariants_json FROM workspace_intents WHERE workspace_id = ?",
            (str(workspace_id),),
        ).fetchone()

        if row is None:
            return ()

        invariants_data = json.loads(row["invariants_json"])
        return tuple(invariants_data.get("invariants", []))

    def _check_invariant(
        self, connection, workspace_id: UUID, invariant: str, change_set: ChangeSet
    ) -> str | None:
        """Check if an invariant is violated by the Change Set."""
        # This is a placeholder for actual invariant checking logic
        # In a real implementation, this would parse and evaluate invariant expressions
        # against the current workspace state and proposed changes

        # For now, return None (no violation) for all invariants
        return None

    def _get_intended_outputs(self, connection, workspace_id: UUID) -> tuple[str, ...]:
        """Retrieve intended output checks for the workspace."""
        # This is a placeholder for intended output definitions
        # In a real implementation, this would be stored in workspace metadata
        return ()

    def _check_intended_output(
        self, connection, workspace_id: UUID, output_check: str, change_set: ChangeSet
    ) -> str | None:
        """Check if intended output is satisfied by the Change Set."""
        # This is a placeholder for actual intended output checking logic
        # In a real implementation, this would verify that operations produce expected outputs
        return None

    def record_validation(
        self, change_set_id: UUID, result: ValidationResult
    ) -> None:
        """Record a validation result for future comparison."""
        with self._database.connection() as connection:
            # Create table if it doesn't exist (this should be in migrations)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS change_set_validations (
                    id TEXT PRIMARY KEY,
                    change_set_id TEXT NOT NULL,
                    validation_result_json TEXT NOT NULL,
                    validated_at TEXT NOT NULL
                )"""
            )

            from uuid import uuid4

            connection.execute(
                """INSERT INTO change_set_validations VALUES (?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    str(change_set_id),
                    json.dumps(result.to_dict()),
                    result.validated_at.isoformat(),
                ),
            )
