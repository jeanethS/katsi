"""Rollback compensation and recovery models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RollbackStepKind(StrEnum):
    """Types of compensation steps during rollback."""

    RESTORE_PREIMAGE = "restore_preimage"
    DELETE_FILE = "delete_file"
    RESTORE_QUARANTINED = "restore_quarantined"
    MOVE_BACK = "move_back"
    DELETE_DIRECTORY = "delete_directory"
    VERSION_RESTORE = "version_restore"


class RollbackStepStatus(StrEnum):
    """Status of an individual rollback step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RollbackStep(BaseModel):
    """Single compensation step in a rollback sequence."""

    id: UUID
    change_set_id: UUID
    step_kind: RollbackStepKind
    ordinal: int = Field(ge=0)  # Execution order (reverse of application)

    status: RollbackStepStatus = RollbackStepStatus.PENDING

    # Step details
    affected_path: str
    preimage_path: str | None = None  # For restores
    preimage_hash: str | None = None
    target_hash: str | None = None  # Expected hash after this step

    # Execution metadata
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None

    # Result verification
    actual_hash: str | None = None
    verified: bool = False


class RollbackJournal(BaseModel):
    """Append-only record of a rollback operation."""

    id: UUID
    change_set_id: UUID
    initiated_at: datetime
    initiated_by: UUID | None = None  # Agent ID or None for system

    total_steps: int = Field(ge=0)
    completed_steps: int = Field(ge=0)
    failed_steps: int = Field(ge=0)

    status: Literal["in_progress", "completed", "failed", "interrupted"] = "in_progress"

    # Recovery metadata
    last_step_ordinal: int | None = None  # For resuming interrupted rollbacks
    interruption_reason: str | None = None

    completed_at: datetime | None = None


class Preimage(BaseModel):
    """Recoverable preimage for rollback compensation."""

    id: UUID
    change_set_id: UUID
    operation_ordinal: int = Field(ge=0)

    original_path: str
    preimage_path: str
    content_hash: str
    byte_count: int = Field(ge=0)

    quarantined: bool = False
    quarantine_path: str | None = None

    created_at: datetime
    expires_at: datetime | None = None  # Optional TTL for cleanup


class RecoveryAnalysis(BaseModel):
    """Startup recovery analysis result."""

    workspace_id: UUID
    change_set_id: UUID
    analyzed_at: datetime

    # State detection
    has_incomplete_apply: bool = False
    has_incomplete_rollback: bool = False
    has_corrupted_preimages: bool = False

    # Safety assessment
    can_safe_apply: bool = False
    can_safe_rollback: bool = False
    can_safe_resume: bool = False

    # Required actions
    requires_owner_intervention: bool = False
    intervention_reason: str | None = None

    # Detected issues
    detected_issues: tuple[str, ...] = ()


class RecoveryRequiredEvidence(BaseModel):
    """Owner-visible evidence when recovery is required."""

    workspace_id: UUID
    change_set_id: UUID
    detected_at: datetime

    situation_type: Literal[
        "incomplete_apply", "incomplete_rollback", "corrupted_preimage", "unknown"
    ]
    description: str = Field(min_length=1, max_length=4_000)

    # What was being done
    operation_in_progress: str | None = None
    steps_completed: int = Field(ge=0)
    total_steps: int = Field(ge=0)

    # What went wrong
    failure_point: str | None = None
    error_message: str | None = None

    # What owner needs to do
    suggested_actions: tuple[str, ...] = ()
    manual_intervention_required: bool = False

    # Evidence for debugging
    journal_snapshot: dict[str, object] = Field(default_factory=dict)
    preimage_status: tuple[str, ...] = ()
    filesystem_state: dict[str, str] = Field(default_factory=dict)


class RollbackCompensation(BaseModel):
    """Compensation instruction derived from a preimage."""

    preimage_id: UUID
    change_set_id: UUID
    operation_ordinal: int = Field(ge=0)

    # Compensation action
    compensation_type: RollbackStepKind
    source_path: str
    target_path: str
    expected_hash: str | None = None

    # Verification after compensation
    verify_hash: bool = True
    verify_exists: bool = True
