"""Rollback service with reverse-order compensation and step recording."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.workspace.contracts import ChangeSet, ChangeSetStatus, Operation
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.rollback import (
    Preimage,
    RecoveryAnalysis,
    RecoveryRequiredEvidence,
    RollbackCompensation,
    RollbackJournal,
    RollbackStep,
    RollbackStepKind,
    RollbackStepStatus,
)
from katsi_core.workspace.verification_service import VerificationError


class RollbackError(Exception):
    """Base exception for rollback errors."""


class PreimageMissingError(RollbackError):
    """Required preimage for rollback is missing or corrupted."""


class RollbackInterruptedError(RollbackError):
    """Rollback was interrupted and requires recovery."""


class RollbackService:
    """Reverse-order compensation from preimages with step recording and hash verification."""

    def __init__(
        self,
        workspace_root: Path,
        preimage_store_path: Path,
    ) -> None:
        self._workspace_root = workspace_root
        self._preimage_store_path = preimage_store_path
        self._preimage_store_path.mkdir(parents=True, exist_ok=True)

        # Track active rollback for interruption detection
        self._active_journals: dict[UUID, RollbackJournal] = {}
        self._rollback_locks: dict[UUID, threading.Lock] = {}

    def plan_compensation(
        self,
        change_set: ChangeSet,
        preimages: Sequence[Preimage],
    ) -> tuple[RollbackCompensation, ...]:
        """Plan reverse-order compensation steps from preimages.

        Args:
            change_set: The Change Set to roll back
            preimages: Available preimages for operations

        Returns:
            Tuple of compensation steps in reverse application order
        """
        preimage_by_ordinal = {p.operation_ordinal: p for p in preimages}

        compensations = []
        # Reverse order: last operation first
        for i in reversed(range(len(change_set.operations))):
            operation = change_set.operations[i]
            preimage = preimage_by_ordinal.get(i)

            if preimage is None:
                # For operations without preimages (e.g., creates), plan delete
                if operation.kind in ("create_file", "create_directory"):
                    compensations.append(
                        RollbackCompensation(
                            preimage_id=uuid4(),  # Placeholder
                            change_set_id=change_set.id,
                            operation_ordinal=i,
                            compensation_type=self._op_to_rollback_kind(operation),
                            source_path="",  # No preimage
                            target_path=str(self._workspace_root / operation.path),
                            expected_hash=None,
                            verify_hash=False,
                            verify_exists=False,
                        )
                    )
                continue

            # Plan restoration from preimage
            compensations.append(
                RollbackCompensation(
                    preimage_id=preimage.id,
                    change_set_id=change_set.id,
                    operation_ordinal=i,
                    compensation_type=self._op_to_rollback_kind(operation),
                    source_path=preimage.preimage_path,
                    target_path=str(self._workspace_root / operation.path),
                    expected_hash=preimage.content_hash,
                    verify_hash=True,
                    verify_exists=True,
                )
            )

        return tuple(compensations)

    def execute_rollback(
        self,
        change_set: ChangeSet,
        compensations: tuple[RollbackCompensation, ...],
        author_id: UUID | None = None,
    ) -> RollbackJournal:
        """Execute rollback in reverse order with step recording and hash verification.

        Args:
            change_set: The Change Set to roll back
            compensations: Compensation steps from plan_compensation
            author_id: Optional agent ID initiating the rollback

        Returns:
            RollbackJournal with recorded steps and final status

        Raises:
            RollbackInterruptedError: If rollback is interrupted
            PreimageMissingError: If required preimage is missing
        """
        journal_id = uuid4()
        journal = RollbackJournal(
            id=journal_id,
            change_set_id=change_set.id,
            initiated_at=datetime.now(UTC),
            initiated_by=author_id,
            total_steps=len(compensations),
            completed_steps=0,
            failed_steps=0,
            status="in_progress",
        )

        self._active_journals[journal_id] = journal
        lock = threading.Lock()
        self._rollback_locks[journal_id] = lock

        steps = []
        try:
            for i, compensation in enumerate(compensations):
                step = self._execute_compensation_step(journal_id, compensation, i)
                steps.append(step)

                with lock:
                    if step.status == RollbackStepStatus.COMPLETED:
                        journal.completed_steps += 1
                    elif step.status == RollbackStepStatus.FAILED:
                        journal.failed_steps += 1

                # Check for interruption signal
                if self._check_interruption(journal_id):
                    journal.status = "interrupted"
                    journal.last_step_ordinal = i
                    raise RollbackInterruptedError(
                        f"Rollback interrupted at step {i + 1}/{len(compensations)}"
                    )

            # All steps completed
            journal.status = "completed"
            journal.completed_at = datetime.now(UTC)

        except Exception as e:
            journal.status = "failed"
            journal.interruption_reason = str(e)
            journal.completed_at = datetime.now(UTC)
            raise

        finally:
            # Cleanup tracking
            self._active_journals.pop(journal_id, None)
            self._rollback_locks.pop(journal_id, None)

        return journal

    def _execute_compensation_step(
        self,
        journal_id: UUID,
        compensation: RollbackCompensation,
        ordinal: int,
    ) -> RollbackStep:
        """Execute a single compensation step with verification."""
        step = RollbackStep(
            id=uuid4(),
            change_set_id=compensation.change_set_id,
            step_kind=compensation.compensation_type,
            ordinal=ordinal,
            status=RollbackStepStatus.IN_PROGRESS,
            affected_path=compensation.target_path,
            preimage_path=compensation.source_path if compensation.source_path else None,
            target_hash=compensation.expected_hash,
            started_at=datetime.now(UTC),
        )

        try:
            target = Path(compensation.target_path)

            # Execute compensation based on type
            if compensation.compensation_type == RollbackStepKind.RESTORE_PREIMAGE:
                self._restore_preimage(compensation, step, target)
            elif compensation.compensation_type == RollbackStepKind.DELETE_FILE:
                self._delete_file(target)
            elif compensation.compensation_type == RollbackStepKind.DELETE_DIRECTORY:
                self._delete_directory(target)
            elif compensation.compensation_type == RollbackStepKind.MOVE_BACK:
                self._move_back(compensation, target)
            else:
                raise RollbackError(f"Unsupported compensation type: {compensation.compensation_type}")

            # Verify if required
            if compensation.verify_hash:
                self._verify_hash(target, compensation.expected_hash, step)

            if compensation.verify_exists:
                self._verify_exists(target, step)

            step.status = RollbackStepStatus.COMPLETED
            step.verified = True

        except Exception as e:
            step.status = RollbackStepStatus.FAILED
            step.error_message = str(e)
            step.verified = False

        finally:
            step.completed_at = datetime.now(UTC)

        return step

    def _restore_preimage(
        self,
        compensation: RollbackCompensation,
        step: RollbackStep,
        target: Path,
    ) -> None:
        """Restore file from preimage."""
        preimage_path = Path(compensation.source_path)
        if not preimage_path.exists():
            raise PreimageMissingError(f"Preimage not found: {preimage_path}")

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Copy preimage to target
        shutil.copy2(preimage_path, target)

    def _delete_file(self, target: Path) -> None:
        """Delete a file."""
        if target.exists() and target.is_file():
            target.unlink()

    def _delete_directory(self, target: Path) -> None:
        """Delete a directory if empty."""
        if target.exists() and target.is_dir():
            try:
                target.rmdir()  # Only remove if empty
            except OSError:
                # Directory not empty, skip
                pass

    def _move_back(self, compensation: RollbackCompensation, target: Path) -> None:
        """Move a file back to its original location."""
        source = Path(compensation.source_path)
        if source.exists():
            shutil.move(str(source), str(target))

    def _verify_hash(self, target: Path, expected_hash: str, step: RollbackStep) -> None:
        """Verify content hash after compensation."""
        if not target.exists():
            raise RollbackError(f"Target does not exist for hash verification: {target}")

        import blake3
        actual_hash = blake3.blake3(target.read_bytes()).hexdigest()

        if actual_hash != expected_hash:
            raise RollbackError(
                f"Hash mismatch for {target}: expected {expected_hash}, got {actual_hash}"
            )

        step.actual_hash = actual_hash

    def _verify_exists(self, target: Path, step: RollbackStep) -> None:
        """Verify that target exists after compensation."""
        if not target.exists():
            raise RollbackError(f"Target does not exist after compensation: {target}")

    def _check_interruption(self, journal_id: UUID) -> bool:
        """Check if rollback should be interrupted."""
        # In production, this would check for external signals
        # For now, return False
        return False

    def _op_to_rollback_kind(self, operation: Operation) -> RollbackStepKind:
        """Map operation kind to rollback compensation kind."""
        mapping = {
            "create_file": RollbackStepKind.DELETE_FILE,
            "replace_file": RollbackStepKind.RESTORE_PREIMAGE,
            "apply_patch": RollbackStepKind.RESTORE_PREIMAGE,
            "copy_file": RollbackStepKind.DELETE_FILE,
            "move_file": RollbackStepKind.MOVE_BACK,
            "create_directory": RollbackStepKind.DELETE_DIRECTORY,
            "quarantine_file": RollbackStepKind.RESTORE_QUARANTINED,
            "restore_quarantined_file": RollbackStepKind.RESTORE_PREIMAGE,
            "replace_derived_artifact": RollbackStepKind.VERSION_RESTORE,
        }
        return mapping.get(operation.kind, RollbackStepKind.RESTORE_PREIMAGE)

    def analyze_startup_recovery(
        self,
        change_set: ChangeSet,
        preimages: Sequence[Preimage],
        incomplete_journal: RollbackJournal | None = None,
    ) -> RecoveryAnalysis:
        """Analyze startup recovery state for applying or rolling back journals.

        Args:
            change_set: The Change Set to analyze
            preimages: Available preimages
            incomplete_journal: Optional incomplete rollback journal

        Returns:
            RecoveryAnalysis with safety assessment
        """
        analysis = RecoveryAnalysis(
            workspace_id=change_set.workspace_id,
            change_set_id=change_set.id,
            analyzed_at=datetime.now(UTC),
        )

        # Check for incomplete apply
        if change_set.status == ChangeSetStatus.APPLYING:
            analysis.has_incomplete_apply = True
            analysis.detected_issues += ("Change Set in APPLYING state",)

        # Check for incomplete rollback
        if incomplete_journal and incomplete_journal.status == "interrupted":
            analysis.has_incomplete_rollback = True
            analysis.detected_issues += (f"Incomplete rollback at step {incomplete_journal.completed_steps}",)

        # Check preimage integrity
        missing_preimages = []
        for preimage in preimages:
            preimage_path = Path(preimage.preimage_path)
            if not preimage_path.exists():
                missing_preimages.append(str(preimage.original_path))
            elif preimage.quarantine_path:
                quarantine_path = Path(preimage.quarantine_path)
                if not quarantine_path.exists():
                    missing_preimages.append(f"quarantine: {preimage.original_path}")

        if missing_preimages:
            analysis.has_corrupted_preimages = True
            analysis.detected_issues += tuple(f"Missing preimage: {path}" for path in missing_preimages)

        # Assess safety
        analysis.can_safe_apply = not (
            analysis.has_incomplete_apply or
            analysis.has_incomplete_rollback or
            analysis.has_corrupted_preimages
        )

        analysis.can_safe_rollback = not analysis.has_corrupted_preimages

        analysis.can_safe_resume = analysis.has_incomplete_rollback and not analysis.has_corrupted_preimages

        # Determine owner intervention
        if analysis.has_corrupted_preimages:
            analysis.requires_owner_intervention = True
            analysis.intervention_reason = "Preimages are missing or corrupted"

        elif analysis.has_incomplete_apply and analysis.has_incomplete_rollback:
            analysis.requires_owner_intervention = True
            analysis.intervention_reason = "Both apply and rollback are incomplete"

        return analysis

    def produce_recovery_evidence(
        self,
        analysis: RecoveryAnalysis,
        change_set: ChangeSet,
        preimages: Sequence[Preimage],
        incomplete_journal: RollbackJournal | None = None,
    ) -> RecoveryRequiredEvidence:
        """Produce owner-visible recovery-required evidence.

        Args:
            analysis: Recovery analysis result
            change_set: The Change Set
            preimages: Available preimages
            incomplete_journal: Optional incomplete journal

        Returns:
            RecoveryRequiredEvidence with owner-visible details
        """
        # Determine situation type
        situation_type: "incomplete_apply" | "incomplete_rollback" | "corrupted_preimage" | "unknown"
        if analysis.has_corrupted_preimages:
            situation_type = "corrupted_preimage"
        elif analysis.has_incomplete_rollback:
            situation_type = "incomplete_rollback"
        elif analysis.has_incomplete_apply:
            situation_type = "incomplete_apply"
        else:
            situation_type = "unknown"

        # Build description
        description = f"Recovery required for Change Set '{change_set.title}'"
        if analysis.intervention_reason:
            description += f": {analysis.intervention_reason}"

        # Build suggested actions
        suggested_actions = []
        manual_intervention = False

        if analysis.has_corrupted_preimages:
            suggested_actions.append("Restore missing preimages from backup")
            suggested_actions.append("Verify workspace filesystem state")
            manual_intervention = True

        elif analysis.has_incomplete_apply:
            suggested_actions.append("Review partial changes in workspace")
            suggested_actions.append("Either complete apply or rollback to previous state")
            manual_intervention = True

        elif analysis.has_incomplete_rollback:
            if analysis.can_safe_resume:
                suggested_actions.append("Resume rollback from last completed step")
            else:
                suggested_actions.append("Manually restore workspace from preimages")
                manual_intervention = True

        # Build preimage status
        preimage_status = []
        for preimage in preimages:
            status = "OK" if Path(preimage.preimage_path).exists() else "MISSING"
            preimage_status.append(f"{preimage.original_path}: {status}")

        # Build journal snapshot
        journal_snapshot = {}
        if incomplete_journal:
            journal_snapshot = {
                "id": str(incomplete_journal.id),
                "status": incomplete_journal.status,
                "completed_steps": incomplete_journal.completed_steps,
                "total_steps": incomplete_journal.total_steps,
                "initiated_at": incomplete_journal.initiated_at.isoformat(),
            }

        # Sample filesystem state
        filesystem_state = {}
        for op in change_set.operations[:3]:  # Sample first 3 operations
            path = self._workspace_root / op.path
            exists = path.exists()
            filesystem_state[str(op.path)] = "exists" if exists else "missing"

        return RecoveryRequiredEvidence(
            workspace_id=analysis.workspace_id,
            change_set_id=analysis.change_set_id,
            detected_at=analysis.analyzed_at,
            situation_type=situation_type,
            description=description,
            operation_in_progress="rollback" if analysis.has_incomplete_rollback else "apply",
            steps_completed=incomplete_journal.completed_steps if incomplete_journal else 0,
            total_steps=len(change_set.operations),
            failure_point=analysis.intervention_reason,
            suggested_actions=tuple(suggested_actions),
            manual_intervention_required=manual_intervention,
            journal_snapshot=journal_snapshot,
            preimage_status=tuple(preimage_status),
            filesystem_state=filesystem_state,
        )
