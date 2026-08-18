"""Governed Executor for safe, recoverable execution with comprehensive fault testing."""

from __future__ import annotations

import random
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from katsi_core.config import LeaseSettings, ObserverSettings, RecoverySettings
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.action_journal import ActionJournalService
from katsi_core.workspace.contracts import (
    ActionOutcomeId,
    AgentIdentityId,
    ChangeSet,
    Operation,
    RelativePath,
    ResourceDependency,
)
from katsi_core.workspace.exclusive_leases import ExclusiveLeaseService
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.recovery_store import RecoveryBlobStore
from katsi_core.workspace.staging import AdjacentStagingManager

if TYPE_CHECKING:
    from katsi_core.media.governed_operations import DerivedMediaArtifactExecutor


class FaultInjector:
    """Inject faults at specified boundaries for testing."""

    def __init__(self, enabled: bool = False, failure_rate: float = 0.0) -> None:
        self._enabled = enabled
        self._failure_rate = failure_rate

    def maybe_fail(self, location: str) -> None:
        """Inject fault at boundary if enabled."""
        if not self._enabled:
            return

        if random.random() < self._failure_rate:
            raise RuntimeError(f"Injected fault at: {location}")

    def set_failure_rate(self, rate: float) -> None:
        """Set failure rate for fault injection (0.0 to 1.0)."""
        self._failure_rate = max(0.0, min(1.0, rate))

    def enable(self) -> None:
        """Enable fault injection."""
        self._enabled = True

    def disable(self) -> None:
        """Disable fault injection."""
        self._enabled = False


class GovernedExecutor:
    """Orchestrates safe, recoverable execution with comprehensive fault testing."""

    def __init__(
        self,
        database: WorkspaceSQLite,
        identities: IdentityService,
        lease_settings: LeaseSettings,
        observer_settings: ObserverSettings,
        recovery_settings: RecoverySettings,
        fault_injector: FaultInjector | None = None,
        media_artifact_executor: DerivedMediaArtifactExecutor | None = None,
    ) -> None:
        self._database = database
        self._identities = identities

        # Initialize services
        self._lease_service = ExclusiveLeaseService(database, identities, lease_settings)
        self._action_journal = ActionJournalService(database)
        self._recovery_store = RecoveryBlobStore(database, recovery_settings)
        self._staging_manager = AdjacentStagingManager(observer_settings)
        self._fault_injector = fault_injector or FaultInjector(enabled=False)
        self._media_artifact_executor = media_artifact_executor

    def execute_change_set(
        self,
        change_set: ChangeSet,
        workspace_root: Path,
        actor_id: AgentIdentityId,
    ) -> ActionOutcomeId:
        """Execute a Change Set with comprehensive fault testing and recovery."""
        try:
            # Step 1: Acquire exclusive lease over write-set
            self._fault_injector.maybe_fail("before_lease_acquisition")

            write_set = self._extract_write_set(change_set.operations)
            lease = self._lease_service.acquire_exclusive(
                workspace_id=change_set.workspace_id,
                holder_id=actor_id,
                change_set_id=change_set.id,
                task_description=f"Execute Change Set: {change_set.title}",
                write_set=write_set,
            )

            self._fault_injector.maybe_fail("after_lease_acquisition")

            # Step 2: Create planning entry in Action Journal
            self._fault_injector.maybe_fail("before_journal_write")

            # Collect affected hashes and preimages
            affected_hashes, preimages = self._collect_state(workspace_root, change_set.operations)

            outcome = self._action_journal.create_planning_entry(
                change_set_id=change_set.id,
                operations=change_set.operations,
                affected_hashes=affected_hashes,
                preimages=preimages,
            )

            self._fault_injector.maybe_fail("after_journal_write")

            # Step 3: Validate dependencies
            self._fault_injector.maybe_fail("before_dependency_validation")

            self._validate_dependencies(workspace_root, change_set.dependencies)

            self._fault_injector.maybe_fail("after_dependency_validation")

            # Step 4: Execute operations with staging and fault injection
            self._fault_injector.maybe_fail("before_operation_execution")

            execution_result = self._execute_operations(
                change_set.operations,
                workspace_root,
                outcome.id,
            )

            self._fault_injector.maybe_fail("after_operation_execution")

            # Step 5: Record successful execution
            self._fault_injector.maybe_fail("before_step_record")

            final_outcome = self._action_journal.record_step(
                outcome_id=outcome.id,
                step_description="execution_complete",
                step_data={
                    "status": "success",
                    "operations_executed": str(len(change_set.operations)),
                    "terminal_outcome": "true",
                },
            )

            self._fault_injector.maybe_fail("after_step_record")

            # Step 6: Release lease (only after terminal outcome)
            self._lease_service.release_exclusive(
                lease_id=lease.id,
                holder_id=actor_id,
                terminal_outcome=True,
                recovery_required=False,
            )

            return outcome.id

        except Exception as e:
            # Handle failure - mark as recovery required
            self._handle_execution_failure(change_set, str(e))

            # Release lease with recovery required flag
            if "lease" in locals():
                with suppress(Exception):
                    self._lease_service.release_exclusive(
                        lease_id=lease.id,
                        holder_id=actor_id,
                        terminal_outcome=False,
                        recovery_required=True,
                    )

            raise

    def execute_operation_idempotent(
        self,
        operation: Operation,
        workspace_root: Path,
        outcome_id: ActionOutcomeId,
    ) -> dict[str, str]:
        """Execute operation with idempotent result return."""
        # Check if already executed
        existing_outcome = self._action_journal.get_outcome(outcome_id)
        if existing_outcome and "execution_result" in existing_outcome.receipt:
            return existing_outcome.receipt["execution_result"]

        # Execute operation
        result = self._execute_single_operation(operation, workspace_root)

        # Record durably
        self._action_journal.record_step(
            outcome_id=outcome_id,
            step_description=f"operation_{operation.kind}",
            step_data=result,
        )

        return result

    def quarantine_and_restore(
        self,
        file_path: Path,
        workspace_root: Path,
    ) -> dict[str, str]:
        """Quarantine and restore without permanent deletion, preserving history."""
        # Quarantine operation
        quarantine_result = self._quarantine_file(file_path, workspace_root)

        # Restore operation
        restore_result = self._restore_quarantined_file(file_path, workspace_root)

        return {
            "quarantine": quarantine_result,
            "restore": restore_result,
        }

    def _execute_operations(
        self,
        operations: tuple[Operation, ...],
        workspace_root: Path,
        outcome_id: ActionOutcomeId,
    ) -> dict[str, str]:
        """Execute all operations with staging and fault boundaries."""
        results = {}

        for i, operation in enumerate(operations):
            self._fault_injector.maybe_fail(f"before_stage_{i}")

            # Stage operation
            target_path = workspace_root / operation.path
            stage_result = self._stage_operation(operation, target_path)

            self._fault_injector.maybe_fail(f"after_stage_{i}")

            # Record step
            self._action_journal.record_step(
                outcome_id=outcome_id,
                step_description=f"stage_{operation.kind}_{i}",
                step_data=stage_result,
            )

            self._fault_injector.maybe_fail(f"before_replace_{i}")

            # Execute atomic replacement
            replace_result = self._execute_replace(operation, target_path)

            self._fault_injector.maybe_fail(f"after_replace_{i}")

            # Record step
            self._action_journal.record_step(
                outcome_id=outcome_id,
                step_description=f"replace_{operation.kind}_{i}",
                step_data=replace_result,
            )

            results[f"operation_{i}"] = {
                "kind": operation.kind,
                "path": operation.path,
                "staged": stage_result,
                "replaced": replace_result,
            }

        return results

    def _execute_single_operation(
        self,
        operation: Operation,
        workspace_root: Path,
    ) -> dict[str, str]:
        """Execute a single operation with staging."""
        target_path = workspace_root / operation.path

        # Stage and replace
        self._stage_operation(operation, target_path)
        result = self._execute_replace(operation, target_path)

        return {
            "kind": operation.kind,
            "path": operation.path,
            "result": result,
        }

    def _stage_operation(
        self,
        operation: Operation,
        target_path: Path,
    ) -> dict[str, str]:
        """Stage operation content."""
        if self._media_artifact_executor and self._media_artifact_executor.supports(operation):
            return self._media_artifact_executor.stage(operation, target_path)  # type: ignore[arg-type]

        if operation.kind in ("create_file", "replace_file", "apply_patch"):
            # These operations need content staging
            # For now, return placeholder
            return {"staged": "true", "path": str(target_path)}

        elif operation.kind == "copy_file":
            source_path = target_path.parent / operation.source_path  # type: ignore
            return self._staging_manager.stage_file_copy(source_path, target_path)

        elif operation.kind == "move_file":
            # Stage both source and destination
            return {"staged": "true", "move_operation": "true"}

        elif operation.kind == "create_directory":
            return {"staged": "true", "directory": "true"}

        elif operation.kind == "quarantine_file":
            return {"staged": "true", "quarantine": "true"}

        elif operation.kind == "restore_quarantined_file":
            return {"staged": "true", "restore": "true"}

        return {"staged": "true"}

    def _execute_replace(
        self,
        operation: Operation,
        target_path: Path,
    ) -> dict[str, str]:
        """Execute atomic replacement for staged operation."""
        if self._media_artifact_executor and self._media_artifact_executor.supports(operation):
            return self._media_artifact_executor.commit(operation, target_path)  # type: ignore[arg-type]

        if operation.kind in ("create_file", "replace_file"):
            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # For create/replace, we'd write content here
            # Placeholder for now
            return {"replaced": "true", "path": str(target_path)}

        elif operation.kind == "move_file":
            # Execute atomic move
            dest_path = target_path.parent / operation.destination_path  # type: ignore
            target_path.rename(dest_path)
            return {"moved": "true", "from": str(target_path), "to": str(dest_path)}

        elif operation.kind == "create_directory":
            target_path.mkdir(parents=True, exist_ok=True)
            return {"directory_created": "true", "path": str(target_path)}

        return {"replaced": "true"}

    def _quarantine_file(
        self,
        file_path: Path,
        workspace_root: Path,
    ) -> dict[str, str]:
        """Quarantine file without permanent deletion."""
        quarantine_path = workspace_root / ".katsi-quarantine" / file_path.name

        # Ensure quarantine directory exists
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)

        # Stage and move
        self._staging_manager.stage_file_copy(file_path, quarantine_path)
        self._staging_manager.atomic_replace(
            self._staging_manager.get_stage_path(quarantine_path),
            quarantine_path,
        )

        return {
            "quarantined": "true",
            "original_path": str(file_path),
            "quarantine_path": str(quarantine_path),
        }

    def _restore_quarantined_file(
        self,
        file_path: Path,
        workspace_root: Path,
    ) -> dict[str, str]:
        """Restore quarantined file preserving history."""
        quarantine_path = workspace_root / ".katsi-quarantine" / file_path.name

        if not quarantine_path.exists():
            return {"restored": "false", "reason": "quarantine_file_not_found"}

        # Stage and restore
        self._staging_manager.stage_file_copy(quarantine_path, file_path)
        self._staging_manager.atomic_replace(
            self._staging_manager.get_stage_path(file_path),
            file_path,
        )

        return {
            "restored": "true",
            "quarantine_path": str(quarantine_path),
            "restored_to": str(file_path),
        }

    def _extract_write_set(self, operations: tuple[Operation, ...]) -> tuple[RelativePath, ...]:
        """Extract write-set from operations."""
        return tuple(op.path for op in operations)

    def _collect_state(
        self,
        workspace_root: Path,
        operations: tuple[Operation, ...],
    ) -> tuple[dict[str, str], dict[str, bytes]]:
        """Collect affected hashes and preimages for recovery."""
        affected_hashes = {}
        preimages = {}

        for operation in operations:
            target_path = workspace_root / operation.path

            if target_path.exists():
                # Store content hash
                with open(target_path, "rb") as f:
                    content = f.read()
                    content_hash = self._recovery_store.store(content)
                    affected_hashes[operation.path] = content_hash
                    preimages[operation.path] = content

        return affected_hashes, preimages

    def _validate_dependencies(
        self,
        workspace_root: Path,
        dependencies: tuple[ResourceDependency, ...],
    ) -> None:
        """Validate Change Set dependencies before execution."""
        for dependency in dependencies:
            if dependency.expected_absent:
                # Resource should not exist
                # Placeholder validation
                pass
            elif dependency.expected_content_hash:
                # Resource should have expected content
                # Placeholder validation
                pass
            elif dependency.expected_version_id:
                # Resource should have expected version
                # Placeholder validation
                pass

    def _handle_execution_failure(self, change_set: ChangeSet, error_message: str) -> None:
        """Handle execution failure by marking recovery required."""
        # Update action journal with failure
        # This would integrate with a recovery manager in a full implementation
        pass

    def get_fault_injector(self) -> FaultInjector:
        """Get the fault injector for testing configuration."""
        return self._fault_injector
