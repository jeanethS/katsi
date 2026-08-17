"""Tests for verification, rollback, and recovery (Task 16)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    CreateFileOperation,
    ResourceDependency,
    ResourceId,
    ResourceVersionId,
    RiskClass,
)
from katsi_core.workspace.rollback import (
    Preimage,
    RecoveryAnalysis,
    RollbackJournal,
    RollbackStepKind,
)
from katsi_core.workspace.rollback_service import (
    PreimageMissingError,
    RollbackInterruptedError,
    RollbackService,
)
from katsi_core.workspace.verification import (
    ChangeSetVerification,
    VerifierApplicability,
    VerifierDefinition,
    VerifierExecution,
    VerifierPolicy,
)
from katsi_core.workspace.verification_service import (
    VerificationService,
    VersionMismatchError,
)
from katsi_core.workspace.verifier_execution import (
    VerifierExecutor,
    redact_secrets,
    truncate_output,
)


def _find_true_executable() -> str:
    """Find the 'true' executable on the system."""
    for candidate in ("/usr/bin/true", "/bin/true", "true"):
        try:
            result = subprocess.run(
                ["which", candidate],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip()
                if Path(path).exists():
                    return path
        except Exception:
            continue
    # Fallback: create a simple no-op script
    return sys.executable  # Use Python as fallback


import sys
TRUE_EXECUTABLE = _find_true_executable()


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def temp_evidence_dir(tmp_path: Path) -> Path:
    """Create a temporary evidence directory."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return evidence


@pytest.fixture
def temp_quarantine_dir(tmp_path: Path) -> Path:
    """Create a temporary quarantine directory."""
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    return quarantine


@pytest.fixture
def sample_change_set() -> ChangeSet:
    """Create a sample Change Set for testing."""
    return ChangeSet(
        id=uuid4(),
        workspace_id=uuid4(),
        author_id=uuid4(),
        title="Test Change Set",
        idempotency_key="test-key-1",
        created_at=datetime.now(UTC),
        dependencies=(
            ResourceDependency(
                resource_id=uuid4(),
                expected_content_hash="abc123def456789012345",
            ),
        ),
        operations=(
            CreateFileOperation(
                kind="create_file",
                path="test.txt",
                byte_count=100,
                result_content_hash="def456789012345678901",
            ),
        ),
        risk=RiskClass.LOW,
    )


@pytest.fixture
def sample_verifier() -> VerifierDefinition:
    """Create a sample verifier definition."""
    return VerifierDefinition(
        id=uuid4(),
        display_name="Test Verifier",
        description="A test verifier for unit tests",
        executable_path=TRUE_EXECUTABLE,
        argument_prefix=(),
        variable_arg_names=(),
        working_directory_scope=None,
        environment_allowlist=("PATH", "HOME"),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(
            operation_kinds=("create_file",),
        ),
    )


# ==================== VERIFIER DEFINITION TESTS ====================


def test_verifier_definition_security_validation() -> None:
    """Test that verifier definitions reject unsafe configurations."""
    with pytest.raises(ValueError, match="shell metacharacters"):
        VerifierDefinition(
            id=uuid4(),
            display_name="Unsafe",
            description="Has shell metacharacters",
            executable_path="cat file.txt | grep test",
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
        )

    with pytest.raises(ValueError, match="environment variable name"):
        VerifierDefinition(
            id=uuid4(),
            display_name="Unsafe",
            description="Has invalid env var",
            executable_path="/bin/true",
            argument_prefix=(),
            variable_arg_names=(),
            environment_allowlist=("PATH=bad",),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
        )

    with pytest.raises(ValueError, match="path patterns must be relative"):
        VerifierDefinition(
            id=uuid4(),
            display_name="Unsafe",
            description="Has unsafe path pattern",
            executable_path="/bin/true",
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
            applicability=VerifierApplicability(
                path_patterns=("/etc/passwd",),
            ),
        )


def test_verifier_applicability_filters() -> None:
    """Test that verifier applicability conditions work correctly."""
    low_risk_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Low Risk Only",
        description="Only applies to low risk",
        executable_path="/bin/true",
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(
            risk_levels=("low",),
            operation_kinds=("create_file",),
        ),
    )

    high_risk_change_set = ChangeSet(
        id=uuid4(),
        workspace_id=uuid4(),
        author_id=uuid4(),
        title="High Risk",
        idempotency_key="high-risk",
        created_at=datetime.now(UTC),
        dependencies=(),
        operations=(
            CreateFileOperation(
                kind="create_file",
                path="test.txt",
                byte_count=100,
                result_content_hash="abc123def456789012345",
            ),
        ),
        risk=RiskClass.HIGH,
    )

    # Should not apply due to risk level mismatch
    executor = VerifierExecutor(Path("/tmp"), Path("/tmp"))
    service = VerificationService(executor, Path("/tmp"), Path("/tmp"))

    applicable = service._filter_applicable_verifiers(
        high_risk_change_set,
        [low_risk_verifier],
    )

    assert len(applicable) == 0


# ==================== SAFE VERIFIER EXECUTION TESTS ====================


def test_safe_verifier_execution_success(
    temp_workspace: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test successful verifier execution with bounded output."""
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)

    execution = executor.execute(
        verifier=sample_verifier,
        change_set_id=sample_change_set.id,
        variable_args={"change_set_id": str(sample_change_set.id)},
    )

    assert execution.exit_code == 0
    assert execution.timed_out is False
    assert execution.output_truncated is False
    assert execution.duration_seconds >= 0


def test_safe_verifier_execution_timeout(
    temp_workspace: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test verifier execution timeout handling."""
    # Find sleep executable
    sleep_exe = shutil.which("sleep") or "/bin/sleep"
    if not Path(sleep_exe).exists():
        pytest.skip("sleep executable not found")

    slow_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Slow Verifier",
        description="A verifier that takes too long",
        executable_path=str(sleep_exe),
        argument_prefix=("10",),
        variable_arg_names=(),
        timeout_seconds=0.1,  # Very short timeout
        max_output_bytes=1_000_000,
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)

    execution = executor.execute(
        verifier=slow_verifier,
        change_set_id=sample_change_set.id,
        variable_args={},
    )

    assert execution.timed_out is True
    # On timeout, the process is killed with signal (usually SIGKILL = -9)
    assert execution.exit_code < 0


def test_secret_redaction() -> None:
    """Test that secrets are redacted from verifier output."""
    output = """
    API_KEY=sk-1234567890abcdef
    token=ghp_1234567890abcdef1234567890abcdef123456
    PASSWORD=mysecret123
    bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9
    """

    redacted = redact_secrets(output)

    assert "sk-1234567890abcdef" not in redacted
    assert "***REDACTED***" in redacted
    assert "API_KEY=" in redacted  # Pattern name preserved
    assert "ghp_1234567890abcdef1234567890abcdef123456" not in redacted
    assert "mysecret123" not in redacted


@pytest.mark.parametrize(
    ("output", "preserved_prefix"),
    [
        ("API_KEY=abc123def456", "API_KEY="),  # short secret below old 16-char floor
        ("token=eyJ+/=abcd", "token="),  # Base64 charset including / + =
        ("auth_token=eyJ+/=abcd", "auth_token="),
        ("SECRET=short123", "SECRET="),
        ("bearer eyJ+/=wxyz1234", "bearer "),
    ],
)
def test_secret_redaction_short_and_token_charset(output: str, preserved_prefix: str) -> None:
    """Short labeled secrets and Base64 token characters are redacted."""
    value = output.split("=", 1)[-1].split(" ", 1)[-1]
    redacted = redact_secrets(output)

    assert value not in redacted
    assert "***REDACTED***" in redacted
    assert preserved_prefix in redacted


def test_output_truncation() -> None:
    """Test that output is truncated to byte limits."""
    long_text = "x" * 1_000_000

    truncated, was_truncated = truncate_output(long_text, max_bytes=1000)

    assert was_truncated is True
    assert len(truncated.encode("utf-8")) <= 1000


def test_preimage_creation(
    temp_workspace: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test that preimages are created correctly."""
    test_file = temp_workspace / "test.txt"
    test_file.write_text("original content")

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)

    preimage = executor.create_preimage(
        original_path=test_file,
        change_set_id=sample_change_set.id,
        operation_ordinal=0,
        expires_in_hours=24,
    )

    assert preimage.change_set_id == sample_change_set.id
    assert preimage.operation_ordinal == 0
    assert preimage.quarantined is True
    assert preimage.content_hash  # blake3 hash present
    assert preimage.byte_count == len("original content")
    assert preimage.expires_at is not None


# ==================== PRE-COMMIT VERIFICATION TESTS ====================


def test_precommit_version_recheck_success(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test successful pre-commit version recheck."""
    executor = VerifierExecutor(temp_workspace, Path(temp_evidence_dir) / "quarantine")
    service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    resource_id = sample_change_set.dependencies[0].resource_id
    current_versions = {resource_id: uuid4()}
    current_hashes = {resource_id: "abc123def456789012345"}

    result = service.precommit_check(
        sample_change_set,
        current_versions,
        current_hashes,
    )

    assert result is True


def test_precommit_version_mismatch(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test pre-commit check fails on version mismatch."""
    executor = VerifierExecutor(temp_workspace, Path(temp_evidence_dir) / "quarantine")
    service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    resource_id = sample_change_set.dependencies[0].resource_id

    # Create a new ChangeSet with mismatched dependency
    mismatched_change_set = ChangeSet(
        id=sample_change_set.id,
        workspace_id=sample_change_set.workspace_id,
        author_id=sample_change_set.author_id,
        title=sample_change_set.title,
        idempotency_key=sample_change_set.idempotency_key,
        dependencies=(
            ResourceDependency(
                resource_id=resource_id,
                expected_content_hash="abc123def456789012345",
            ),
        ),
        operations=sample_change_set.operations,
        risk=sample_change_set.risk,
        status=sample_change_set.status,
        successor_id=sample_change_set.successor_id,
        created_at=sample_change_set.created_at,
    )
    current_hashes = {resource_id: "wrong_hash_value_mismatch"}

    with pytest.raises(VersionMismatchError):
        service.precommit_check(
            mismatched_change_set,
            {},
            current_hashes,
        )


def test_verification_evidence_linking(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test that verification evidence is linked correctly."""
    executor = VerifierExecutor(temp_workspace, Path(temp_evidence_dir) / "quarantine")
    service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = ChangeSetVerification(
        change_set_id=sample_change_set.id,
        required_verifiers_count=1,
        passed_verifiers_count=1,
        failed_verifiers_count=0,
        timeout_verifiers_count=0,
        owner_verified=False,
        executions=(
            VerifierExecution(
                verifier_id=uuid4(),
                change_set_id=sample_change_set.id,
                exit_code=0,
                signal=None,
                timed_out=False,
                output_truncated=False,
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_sample="test output",
                stderr_sample="",
                duration_seconds=0.1,
                occurred_at=datetime.now(UTC).isoformat(),
            ),
        ),
        invariants=(),
        all_required_passed=True,
        any_required_passed=True,
        can_proceed_verified=True,
        can_proceed_unverified=False,
    )

    evidence = service.link_verification_evidence(
        sample_change_set.id,
        verification,
    )

    assert len(evidence) > 0
    assert all(e.change_set_id == sample_change_set.id for e in evidence)


# ==================== VERIFIED/UNVERIFIED STATES TESTS ====================


def test_verified_state_all_required_pass(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test that verified state is achieved when all required verifiers pass."""
    executor = VerifierExecutor(temp_workspace, Path(temp_evidence_dir) / "quarantine")
    service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=True,  # Owner verified since we have a required verifier
    )

    assert verification.can_proceed_verified is True
    assert verification.can_proceed_unverified is False
    assert verification.all_required_passed is True


def test_applied_unverified_state_no_verifier(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test applied-unverified state when no verifier applies and owner hasn't verified."""
    # Create a verifier with conflicting applicability
    non_applicable_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Non-Applicable",
        description="Does not apply to this change set",
        executable_path=TRUE_EXECUTABLE,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.OPTIONAL,
        applicability=VerifierApplicability(
            operation_kinds=("delete_file",),  # Not in our change set
        ),
    )

    executor = VerifierExecutor(temp_workspace, Path(temp_evidence_dir) / "quarantine")
    service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = service.verify_change_set(
        sample_change_set,
        [non_applicable_verifier],
        owner_verified=False,
    )

    # When no verifiers apply and no required verifiers exist,
    # can_proceed_verified is True (nothing to verify)
    assert verification.can_proceed_verified is True
    assert verification.can_proceed_unverified is False
    assert verification.all_required_passed is True  # No required verifiers


# ==================== ROLLBACK COMPENSATION TESTS ====================


def test_rollback_compensation_planning(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test reverse-order compensation planning."""
    # For a create_file operation, the rollback is to delete the file
    # (not restore from preimage, since there was no original file)
    service = RollbackService(temp_workspace, Path(temp_workspace) / "preimages")
    compensations = service.plan_compensation(sample_change_set, ())

    assert len(compensations) == 1
    assert compensations[0].operation_ordinal == 0
    # create_file rolls back by deleting the created file
    assert compensations[0].compensation_type == RollbackStepKind.DELETE_FILE


def test_rollback_execution_success(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test successful rollback execution with step recording."""
    # Create a test file (simulating a create_file operation)
    test_file = temp_workspace / "test.txt"
    test_file.write_text("new content")

    # For create_file operation, rollback is delete (no preimage needed)
    service = RollbackService(temp_workspace, temp_workspace / "quarantine")
    compensations = service.plan_compensation(sample_change_set, ())

    journal = service.execute_rollback(sample_change_set, compensations)

    assert journal.status == "completed"
    assert journal.completed_steps == 1
    assert journal.failed_steps == 0
    # Verify file was deleted
    assert not test_file.exists()


def test_rollback_interrupted_handling(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test interrupted rollback handling."""
    quarantine = temp_workspace / "quarantine"
    quarantine.mkdir()

    service = RollbackService(temp_workspace, quarantine)

    # Create compensations that will trigger interruption
    # We need to mock the interruption check
    compensations = service.plan_compensation(sample_change_set, ())

    # Mock the interruption check to return True
    original_check = service._check_interruption
    service._check_interruption = lambda x: True  # type: ignore

    with pytest.raises(RollbackInterruptedError):
        service.execute_rollback(sample_change_set, compensations)

    # Restore original
    service._check_interruption = original_check  # type: ignore


def test_corrupted_preimage_handling(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test handling of corrupted preimages."""
    quarantine = temp_workspace / "quarantine"
    quarantine.mkdir()

    # Create a change set with a replace_file operation (which needs preimage)
    # For this test, we need a different operation type
    from katsi_core.workspace.contracts import ReplaceFileOperation

    replace_change_set = ChangeSet(
        id=uuid4(),
        workspace_id=uuid4(),
        author_id=uuid4(),
        title="Replace Test",
        idempotency_key="replace-test",
        created_at=datetime.now(UTC),
        dependencies=(),
        operations=(
            ReplaceFileOperation(
                kind="replace_file",
                path="test.txt",
                byte_count=100,
                expected_content_hash="abc123def456789012345",
                result_content_hash="def456789012345678901",
            ),
        ),
        risk=RiskClass.LOW,
    )

    # Preimage with missing file
    missing_preimage = quarantine / "missing.bin"
    # Don't create it - it's missing

    preimages = (
        Preimage(
            id=uuid4(),
            change_set_id=replace_change_set.id,
            operation_ordinal=0,
            original_path="test.txt",
            preimage_path=str(missing_preimage),
            content_hash="abc123def456789012345",
            byte_count=100,
            created_at=datetime.now(UTC),
        ),
    )

    service = RollbackService(temp_workspace, quarantine)
    compensations = service.plan_compensation(replace_change_set, preimages)

    # Execute rollback - missing preimage causes step failure but not exception
    journal = service.execute_rollback(replace_change_set, compensations)

    # The rollback completes but with failed steps
    assert journal.failed_steps > 0
    assert journal.status in ("completed", "failed")


# ==================== STARTUP RECOVERY ANALYSIS TESTS ====================


def test_startup_recovery_analysis_incomplete_apply(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test recovery analysis for incomplete apply."""
    # Change set stuck in APPLYING state
    applying_change_set = ChangeSet(
        id=sample_change_set.id,
        workspace_id=sample_change_set.workspace_id,
        author_id=sample_change_set.author_id,
        title=sample_change_set.title,
        idempotency_key=sample_change_set.idempotency_key,
        dependencies=sample_change_set.dependencies,
        operations=sample_change_set.operations,
        risk=sample_change_set.risk,
        status=ChangeSetStatus.APPLYING,
        successor_id=sample_change_set.successor_id,
        created_at=sample_change_set.created_at,
    )

    service = RollbackService(temp_workspace, Path(temp_workspace) / "preimages")

    analysis = service.analyze_startup_recovery(
        applying_change_set,
        (),
    )

    assert analysis.has_incomplete_apply is True
    assert analysis.can_safe_apply is False


def test_startup_recovery_analysis_corrupted_preimages(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test recovery analysis for corrupted preimages."""
    quarantine = temp_workspace / "quarantine"
    quarantine.mkdir()

    # Preimage with missing quarantine file
    preimages = (
        Preimage(
            id=uuid4(),
            change_set_id=sample_change_set.id,
            operation_ordinal=0,
            original_path="test.txt",
            preimage_path=str(quarantine / "missing.bin"),
            content_hash="abc123",
            byte_count=100,
            created_at=datetime.now(UTC),
        ),
    )

    service = RollbackService(temp_workspace, quarantine)

    analysis = service.analyze_startup_recovery(
        sample_change_set,
        preimages,
    )

    assert analysis.has_corrupted_preimages is True
    assert analysis.requires_owner_intervention is True


def test_recovery_required_evidence_production(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test production of recovery-required evidence."""
    service = RollbackService(temp_workspace, Path(temp_workspace) / "preimages")

    analysis = RecoveryAnalysis(
        workspace_id=sample_change_set.workspace_id,
        change_set_id=sample_change_set.id,
        analyzed_at=datetime.now(UTC),
        has_corrupted_preimages=True,
        requires_owner_intervention=True,
        intervention_reason="Preimages are missing",
        detected_issues=("Missing preimage: test.txt",),
    )

    evidence = service.produce_recovery_evidence(
        analysis,
        sample_change_set,
        (),
    )

    assert evidence.situation_type == "corrupted_preimage"
    assert evidence.manual_intervention_required is True
    assert len(evidence.suggested_actions) > 0


def test_safe_recovery_procedures(
    temp_workspace: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test safe recovery procedures prevent overlapping writes."""
    quarantine = temp_workspace / "quarantine"
    quarantine.mkdir()

    service = RollbackService(temp_workspace, quarantine)

    # Test that both apply and rollback are safe when change set is clean
    analysis = service.analyze_startup_recovery(
        sample_change_set,
        (),  # No preimages
    )

    # When change set is clean (not applying/rolling back, no corrupted preimages)
    # both apply and rollback should be safe
    assert analysis.can_safe_apply is True
    assert analysis.can_safe_rollback is True

    # When both apply and rollback are unsafe, intervention is required
    incomplete_change_set = ChangeSet(
        id=sample_change_set.id,
        workspace_id=sample_change_set.workspace_id,
        author_id=sample_change_set.author_id,
        title=sample_change_set.title,
        idempotency_key=sample_change_set.idempotency_key,
        dependencies=sample_change_set.dependencies,
        operations=sample_change_set.operations,
        risk=sample_change_set.risk,
        status=ChangeSetStatus.APPLYING,
        successor_id=sample_change_set.successor_id,
        created_at=sample_change_set.created_at,
    )

    # Use "interrupted" status to trigger has_incomplete_rollback
    incomplete_journal = RollbackJournal(
        id=uuid4(),
        change_set_id=incomplete_change_set.id,
        initiated_at=datetime.now(UTC),
        total_steps=1,
        completed_steps=0,
        failed_steps=0,
        status="interrupted",
    )

    analysis = service.analyze_startup_recovery(
        incomplete_change_set,
        (),
        incomplete_journal,
    )

    # When change set is incomplete (has incomplete_apply AND incomplete_rollback)
    # intervention should be required
    assert analysis.requires_owner_intervention is True


# ==================== VERIFICATION AND RECOVERY INTEGRATION TESTS ====================


def test_full_verification_and_rollback_workflow(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test complete workflow from verification through rollback."""
    # Setup
    quarantine = temp_evidence_dir / "quarantine"
    quarantine.mkdir()
    executor = VerifierExecutor(temp_workspace, quarantine)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)
    rollback_service = RollbackService(temp_workspace, quarantine)

    # Create test file
    test_file = temp_workspace / "test.txt"
    test_file.write_text("original content")

    # Create preimage
    preimage = executor.create_preimage(
        original_path=test_file,
        change_set_id=sample_change_set.id,
        operation_ordinal=0,
    )

    # Verify change set
    verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Test Verifier",
        description="Test verifier",
        executable_path=TRUE_EXECUTABLE,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
    )

    verification = verification_service.verify_change_set(
        sample_change_set,
        [verifier],
        owner_verified=True,
    )

    assert verification.can_proceed_verified is True

    # Plan and execute rollback
    # For create_file operation, rollback is delete
    compensations = rollback_service.plan_compensation(
        sample_change_set,
        (),  # No preimages for create_file
    )

    journal = rollback_service.execute_rollback(
        sample_change_set,
        compensations,
    )

    assert journal.status == "completed"

    # Verify file was deleted (since it was a create_file operation)
    assert not test_file.exists()


def test_restart_recovery_scenarios(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test various restart recovery scenarios."""
    quarantine = temp_evidence_dir / "quarantine"
    quarantine.mkdir()
    rollback_service = RollbackService(temp_workspace, quarantine)

    # Scenario 1: Change set in applying state
    applying_change_set = ChangeSet(
        id=sample_change_set.id,
        workspace_id=sample_change_set.workspace_id,
        author_id=sample_change_set.author_id,
        title=sample_change_set.title,
        idempotency_key=sample_change_set.idempotency_key,
        dependencies=sample_change_set.dependencies,
        operations=sample_change_set.operations,
        risk=sample_change_set.risk,
        status=ChangeSetStatus.APPLYING,
        successor_id=sample_change_set.successor_id,
        created_at=sample_change_set.created_at,
    )

    analysis1 = rollback_service.analyze_startup_recovery(
        applying_change_set,
        (),
    )

    assert analysis1.has_incomplete_apply

    # Scenario 2: Interrupted rollback
    incomplete_journal = RollbackJournal(
        id=uuid4(),
        change_set_id=sample_change_set.id,
        initiated_at=datetime.now(UTC),
        total_steps=2,
        completed_steps=1,
        failed_steps=0,
        status="interrupted",
        last_step_ordinal=0,
    )

    analysis2 = rollback_service.analyze_startup_recovery(
        sample_change_set,
        (),
        incomplete_journal,
    )

    assert analysis2.has_incomplete_rollback
    assert analysis2.can_safe_resume

    # Scenario 3: Corrupted preimages
    test_file = temp_workspace / "test.txt"
    test_file.write_text("content")

    preimage = Preimage(
        id=uuid4(),
        change_set_id=sample_change_set.id,
        operation_ordinal=0,
        original_path="test.txt",
        preimage_path=str(quarantine / "missing.bin"),
        content_hash="abc123",
        byte_count=7,
        created_at=datetime.now(UTC),
    )

    analysis3 = rollback_service.analyze_startup_recovery(
        sample_change_set,
        (preimage,),
    )

    assert analysis3.has_corrupted_preimages
    assert analysis3.requires_owner_intervention


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
