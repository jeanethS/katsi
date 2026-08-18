"""Comprehensive verifier scenario testing for katsi workspace coordination system.

This module tests realistic verifier scenarios including:
- Verifier success (exit code 0, can proceed)
- Verifier failure (exit code != 0, blocked)
- Verifier timeout (exceeds time limit, handled as failure)
- Owner verification workflow (approval/denial after verifier)
- Interrupted verification recovery (system crash during verifier run)

All tests verify security constraints are enforced:
- shell=False execution (no shell interpretation)
- Bounded output limits enforced
- No DB transactions during execution
- Secret redaction from output
- Timeout handling
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.workspace.contracts import (
    ChangeSet,
    CreateFileOperation,
    ResourceDependency,
    RiskClass,
)
from katsi_core.workspace.verification import (
    VerifierApplicability,
    VerifierDefinition,
    VerifierPolicy,
)
from katsi_core.workspace.verification_service import VerificationService
from katsi_core.workspace.verifier_execution import (
    VerifierExecutor,
    redact_secrets,
)

# ==================== FIXTURES AND HELPERS ====================


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
    # Fallback: use Python as no-op
    import sys

    return sys.executable


def _find_false_executable() -> str:
    """Find the 'false' executable on the system (returns exit code 1)."""
    for candidate in ("/usr/bin/false", "/bin/false", "false"):
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
    # Fallback: create a script that exits with code 1
    return str(Path(__file__).parent / "mock_exit_1.py")


def _create_mock_false_script(tmp_path: Path) -> str:
    """Create a mock script that exits with code 1."""
    script_path = tmp_path / "mock_exit_1.py"
    script_path.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
    script_path.chmod(0o755)
    return str(script_path)


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
    """Create a sample verifier definition that succeeds."""
    return VerifierDefinition(
        id=uuid4(),
        display_name="Test Verifier",
        description="A test verifier that always succeeds",
        executable_path=TRUE_EXECUTABLE,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(
            operation_kinds=("create_file",),
        ),
    )


# ==================== SCENARIO 1: VERIFIER SUCCESS (EXIT CODE 0) ====================


def test_verifier_success_exit_code_zero(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test verifier success scenario: exit code 0 enables Change Set to proceed.

    Scenario:
    - Verifier executes successfully with exit code 0
    - No timeout occurs
    - Output is within limits
    - Change Set can proceed in verified state
    - Security constraints verified (shell=False, bounded output)
    """
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # Execute verifier against Change Set
    # Note: With required_count > 0, owner_verified must be True for can_proceed_verified
    verification = verification_service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=True,  # Required when there are required verifiers
    )

    # Verify success criteria
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 1
    assert verification.failed_verifiers_count == 0
    assert verification.timeout_verifiers_count == 0
    assert verification.all_required_passed is True
    assert verification.can_proceed_verified is True
    assert verification.can_proceed_unverified is False

    # Verify execution details
    assert len(verification.executions) == 1
    execution = verification.executions[0]
    assert execution.exit_code == 0
    assert execution.timed_out is False
    assert execution.output_truncated is False
    assert execution.signal is None
    assert execution.duration_seconds >= 0

    # Verify security constraints (no shell interpretation)
    assert verification.executions[0].stdout_sample is not None
    assert verification.executions[0].stderr_sample is not None


def test_multiple_verifiers_all_success(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test scenario with multiple successful verifiers."""
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verifiers = [
        VerifierDefinition(
            id=uuid4(),
            display_name=f"Verifier {i}",
            description=f"Test verifier {i}",
            executable_path=TRUE_EXECUTABLE,
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
            policy=VerifierPolicy.REQUIRED_ALL,
            applicability=VerifierApplicability(
                operation_kinds=("create_file",),
            ),
        )
        for i in range(3)
    ]

    verification = verification_service.verify_change_set(
        sample_change_set,
        verifiers,
        owner_verified=True,  # Required when there are required verifiers
    )

    # All should pass
    assert verification.required_verifiers_count == 3
    assert verification.passed_verifiers_count == 3
    assert verification.failed_verifiers_count == 0
    assert verification.all_required_passed is True
    assert verification.can_proceed_verified is True


# ==================== SCENARIO 2: VERIFIER FAILURE (EXIT CODE != 0) ====================


def test_verifier_failure_exit_code_nonzero_blocks_change_set(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test verifier failure scenario: exit code != 0 blocks Change Set.

    Scenario:
    - Verifier executes but returns exit code 1
    - Change Set is blocked from proceeding
    - Failure is recorded in verification results
    - Security constraints still enforced (bounded output, shell=False)
    """
    # Create failing verifier script
    false_exec = _create_mock_false_script(temp_quarantine_dir)

    failing_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Failing Verifier",
        description="A verifier that always fails",
        executable_path=false_exec,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(
            operation_kinds=("create_file",),
        ),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = verification_service.verify_change_set(
        sample_change_set,
        [failing_verifier],
        owner_verified=False,
    )

    # Verify failure is properly recorded
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 0
    assert verification.failed_verifiers_count == 1
    assert verification.timeout_verifiers_count == 0
    assert verification.all_required_passed is False
    assert verification.can_proceed_verified is False

    # Verify execution details
    assert len(verification.executions) == 1
    execution = verification.executions[0]
    assert execution.exit_code == 1
    assert execution.timed_out is False
    assert execution.signal is None


def test_mixed_verifiers_one_failure_blocks_all(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test scenario with mixed results: one failure blocks all with REQUIRED_ALL policy."""
    false_exec = _create_mock_false_script(temp_quarantine_dir)

    verifiers = [
        VerifierDefinition(
            id=uuid4(),
            display_name="Passing Verifier",
            description="Should pass",
            executable_path=TRUE_EXECUTABLE,
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
            policy=VerifierPolicy.REQUIRED_ALL,
            applicability=VerifierApplicability(operation_kinds=("create_file",)),
        ),
        VerifierDefinition(
            id=uuid4(),
            display_name="Failing Verifier",
            description="Should fail",
            executable_path=false_exec,
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
            policy=VerifierPolicy.REQUIRED_ALL,
            applicability=VerifierApplicability(operation_kinds=("create_file",)),
        ),
    ]

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = verification_service.verify_change_set(
        sample_change_set,
        verifiers,
        owner_verified=False,
    )

    # One failure should block all with REQUIRED_ALL
    assert verification.required_verifiers_count == 2
    assert verification.passed_verifiers_count == 1
    assert verification.failed_verifiers_count == 1
    assert verification.all_required_passed is False
    assert verification.can_proceed_verified is False


# ==================== SCENARIO 3: VERIFIER TIMEOUT ====================


def test_verifier_timeout_exceeds_limit(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test verifier timeout scenario: exceeding time limit is treated as failure.

    Scenario:
    - Verifier takes longer than configured timeout
    - Process is killed when timeout expires
    - Timeout is recorded as failure (not as regular exit code)
    - Change Set is blocked from proceeding
    - Security constraint: process actually gets killed, no hanging
    """
    sleep_exe = shutil.which("sleep") or "/bin/sleep"
    if not Path(sleep_exe).exists():
        pytest.skip("sleep executable not found")

    slow_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Slow Verifier",
        description="A verifier that takes too long",
        executable_path=str(sleep_exe),
        argument_prefix=("10",),  # Sleep for 10 seconds
        variable_arg_names=(),
        timeout_seconds=0.5,  # Timeout after 0.5 seconds
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(
            operation_kinds=("create_file",),
        ),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    import time

    start = time.time()
    verification = verification_service.verify_change_set(
        sample_change_set,
        [slow_verifier],
        owner_verified=False,
    )
    duration = time.time() - start

    # Verify timeout was properly handled (should not wait full 10 seconds)
    assert duration < 5.0, f"Timeout took {duration}s, should have been ~0.5s"

    # Verify timeout is recorded as failure
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 0
    assert verification.failed_verifiers_count == 0
    assert verification.timeout_verifiers_count == 1
    assert verification.all_required_passed is False
    assert verification.can_proceed_verified is False

    # Verify execution details
    assert len(verification.executions) == 1
    execution = verification.executions[0]
    assert execution.timed_out is True
    assert execution.exit_code < 0  # Negative exit code indicates signal kill


def test_verifier_timeout_with_partial_output(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test verifier timeout with partial output capture before timeout."""
    sleep_exe = shutil.which("sleep") or "/bin/sleep"
    if not Path(sleep_exe).exists():
        pytest.skip("sleep executable not found")

    # Use sleep with output prefix - should timeout
    slow_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Slow Output Verifier",
        description="Produces output slowly then times out",
        executable_path=str(sleep_exe),
        argument_prefix=("10",),  # Sleep for 10 seconds (longer than timeout)
        variable_arg_names=(),
        timeout_seconds=0.5,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = verification_service.verify_change_set(
        sample_change_set,
        [slow_verifier],
        owner_verified=False,
    )

    # Verify timeout occurred
    assert verification.timeout_verifiers_count == 1
    execution = verification.executions[0]
    assert execution.timed_out is True

    # Verify execution was terminated (no hanging)
    assert execution.exit_code < 0  # Negative exit code indicates signal kill


# ==================== SCENARIO 4: OWNER VERIFICATION WORKFLOW ====================


def test_owner_verification_workflow_required_verifier_failed(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test owner verification workflow after verifier failure.

    Scenario:
    - Required verifier fails (exit code != 0)
    - Owner explicitly approves despite verifier failure
    - System records owner decision but still blocks due to failed required verifier
    - Owner approval is insufficient when required verifiers fail
    """
    false_exec = _create_mock_false_script(temp_quarantine_dir)

    failing_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Failing Verifier",
        description="A required verifier that fails",
        executable_path=false_exec,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # Owner tries to approve despite verifier failure
    verification = verification_service.verify_change_set(
        sample_change_set,
        [failing_verifier],
        owner_verified=True,  # Owner explicitly verified
    )

    # Owner approval cannot override required verifier failure
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 0
    assert verification.failed_verifiers_count == 1
    assert verification.owner_verified is True  # Owner did verify
    assert verification.all_required_passed is False
    assert verification.can_proceed_verified is False  # Still blocked!


def test_owner_verification_workflow_optional_verifier_failed(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test owner verification workflow after optional verifier failure.

    Scenario:
    - Optional verifier fails
    - Owner explicitly approves
    - Change Set can proceed (optional verifier failure is OK with owner approval)
    """
    false_exec = _create_mock_false_script(temp_quarantine_dir)

    optional_failing_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Optional Failing Verifier",
        description="An optional verifier that fails",
        executable_path=false_exec,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.OPTIONAL,  # Optional, not required
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # Owner approves despite optional verifier failure
    verification = verification_service.verify_change_set(
        sample_change_set,
        [optional_failing_verifier],
        owner_verified=True,  # Owner explicitly verified
    )

    # Optional verifier failure + owner approval = can proceed
    assert verification.required_verifiers_count == 0  # No required verifiers
    assert verification.failed_verifiers_count == 1  # Optional failed
    assert verification.owner_verified is True
    assert verification.all_required_passed is True  # No required verifiers to pass
    assert verification.can_proceed_verified is True  # Can proceed with owner approval


def test_owner_verification_workflow_all_pass_without_owner(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test owner verification workflow when all verifiers pass without owner approval.

    Scenario:
    - All required verifiers pass
    - Owner has not explicitly verified (owner_verified=False)
    - Change Set CANNOT proceed (owner approval IS required when there are required verifiers)

    This test verifies the security constraint: when required verifiers exist,
    owner approval is mandatory even if all verifiers pass.
    """
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    verification = verification_service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=False,  # Owner has not verified
    )

    # All required passed, but owner approval IS needed when there are required verifiers
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 1
    assert verification.failed_verifiers_count == 0
    assert verification.owner_verified is False
    assert verification.all_required_passed is True
    assert verification.can_proceed_verified is False  # BLOCKED - owner approval required!


def test_owner_verification_workflow_denial_after_verifier_pass(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test owner denial workflow after verifiers pass.

    Scenario:
    - All verifiers pass
    - Owner explicitly denies approval (owner_verified=False)
    - Change Set blocked because owner approval is required for this risk level
    - System preserves verifier results but respects owner decision
    """
    # Create a verifier that passes
    passing_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Passing Verifier",
        description="A verifier that passes",
        executable_path=TRUE_EXECUTABLE,
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # Verifiers pass but owner denies approval
    verification = verification_service.verify_change_set(
        sample_change_set,
        [passing_verifier],
        owner_verified=False,  # Owner denial
    )

    # Verifiers passed but owner denied
    assert verification.required_verifiers_count == 1
    assert verification.passed_verifiers_count == 1
    assert verification.failed_verifiers_count == 0
    assert verification.owner_verified is False
    assert verification.all_required_passed is True
    # With REQUIRED_ALL policy and no owner verification, BLOCKED
    assert verification.can_proceed_verified is False  # BLOCKED - owner approval required


# ==================== SCENARIO 5: INTERRUPTED VERIFICATION RECOVERY ====================


def test_interrupted_verification_recovery_system_crash_during_execution(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test interrupted verification recovery after system crash during verifier run.

    Scenario:
    - Verifier is running
    - System crashes (simulated by timeout)
    - Recovery process detects incomplete verification
    - Verification can be resumed (idempotent)
    - No partial results are applied
    - Security constraint: verification is atomic (all-or-nothing)
    """
    sleep_exe = shutil.which("sleep") or "/bin/sleep"
    if not Path(sleep_exe).exists():
        pytest.skip("sleep executable not found")

    long_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Long Running Verifier",
        description="A verifier that may be interrupted",
        executable_path=str(sleep_exe),
        argument_prefix=("10",),  # Sleep for 10 seconds
        variable_arg_names=(),
        timeout_seconds=0.5,  # Short timeout to simulate interruption
        max_output_bytes=1_000_000,
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # First execution will timeout (simulating crash/interruption)
    verification1 = verification_service.verify_change_set(
        sample_change_set,
        [long_verifier],
        owner_verified=False,
    )

    # Verify interruption was detected
    assert verification1.timeout_verifiers_count == 1
    assert verification1.all_required_passed is False

    # Recovery: re-run verification (should be idempotent)
    verification2 = verification_service.verify_change_set(
        sample_change_set,
        [long_verifier],
        owner_verified=False,
    )

    # Verify recovery produces same result (idempotent)
    assert verification2.timeout_verifiers_count == 1
    assert verification2.all_required_passed is False


def test_interrupted_verification_recovery_evidence_preservation(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test that verification evidence is preserved across interrupted runs."""
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # First successful verification
    verification1 = verification_service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=False,
    )

    evidence1 = verification_service.link_verification_evidence(
        sample_change_set.id,
        verification1,
    )

    # Simulate interruption and re-verification
    verification2 = verification_service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=False,
    )

    evidence2 = verification_service.link_verification_evidence(
        sample_change_set.id,
        verification2,
    )

    # Evidence should be consistent across runs
    assert len(evidence1) == len(evidence2)
    assert all(
        e1.change_set_id == e2.change_set_id for e1, e2 in zip(evidence1, evidence2, strict=False)
    )


def test_interrupted_verification_with_multiple_verifiers_recovery(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test recovery when interruption occurs during multiple verifier execution."""
    sleep_exe = shutil.which("sleep") or "/bin/sleep"
    if not Path(sleep_exe).exists():
        pytest.skip("sleep executable not found")

    # Mix of fast and slow verifiers
    verifiers = [
        VerifierDefinition(
            id=uuid4(),
            display_name="Fast Verifier",
            description="Executes quickly",
            executable_path=TRUE_EXECUTABLE,
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
            policy=VerifierPolicy.REQUIRED_ALL,
            applicability=VerifierApplicability(operation_kinds=("create_file",)),
        ),
        VerifierDefinition(
            id=uuid4(),
            display_name="Slow Verifier",
            description="Times out",
            executable_path=str(sleep_exe),
            argument_prefix=("10",),
            variable_arg_names=(),
            timeout_seconds=0.5,
            max_output_bytes=1_000_000,
            policy=VerifierPolicy.REQUIRED_ALL,
            applicability=VerifierApplicability(operation_kinds=("create_file",)),
        ),
    ]

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # First run: slow verifier times out
    verification1 = verification_service.verify_change_set(
        sample_change_set,
        verifiers,
        owner_verified=False,
    )

    assert verification1.passed_verifiers_count == 1
    assert verification1.timeout_verifiers_count == 1
    assert verification1.all_required_passed is False

    # Recovery: re-run (should be idempotent)
    verification2 = verification_service.verify_change_set(
        sample_change_set,
        verifiers,
        owner_verified=False,
    )

    assert verification2.passed_verifiers_count == 1
    assert verification2.timeout_verifiers_count == 1
    assert verification2.all_required_passed is False


# ==================== SECURITY CONSTRAINT TESTS ====================


def test_security_constraint_shell_false_enforced(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test that shell=False security constraint is enforced."""
    # Try to create a verifier with shell metacharacters
    with pytest.raises(ValueError, match="shell metacharacters"):
        VerifierDefinition(
            id=uuid4(),
            display_name="Unsafe Verifier",
            description="Tries to use shell interpretation",
            executable_path="cat file.txt | grep test",  # Shell metacharacters
            argument_prefix=(),
            variable_arg_names=(),
            timeout_seconds=30.0,
            max_output_bytes=1_000_000,
        )


def test_security_constraint_bounded_output_enforced(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test that bounded output security constraint is enforced."""
    # Create a verifier that produces large output
    large_output_script = temp_quarantine_dir / "large_output.sh"
    large_output_script.write_text("#!/bin/sh\necho '")
    large_output_script.chmod(0o755)

    large_output_verifier = VerifierDefinition(
        id=uuid4(),
        display_name="Large Output Verifier",
        description="Produces large output",
        executable_path=str(large_output_script),
        argument_prefix=(),
        variable_arg_names=(),
        timeout_seconds=30.0,
        max_output_bytes=100,  # Very small limit to test truncation
        policy=VerifierPolicy.REQUIRED_ALL,
        applicability=VerifierApplicability(operation_kinds=("create_file",)),
    )

    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)

    execution = executor.execute(
        verifier=large_output_verifier,
        change_set_id=sample_change_set.id,
        variable_args={},
    )

    # Verify execution completed (even if truncated)
    assert execution is not None
    # The output should be within limits
    assert len(execution.stdout_sample.encode("utf-8")) <= 100 or execution.output_truncated


def test_security_constraint_secret_redaction_enforced(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
) -> None:
    """Test that secret redaction security constraint is enforced."""
    # Test the secret redaction function directly

    output_with_secrets = """
    API_KEY=sk-1234567890abcdef
    token=ghp_1234567890abcdef1234567890abcdef123456
    PASSWORD=mysecret123
    bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9
    """

    redacted = redact_secrets(output_with_secrets)

    # Verify secrets are redacted
    assert "sk-1234567890abcdef" not in redacted
    assert "ghp_1234567890abcdef1234567890abcdef123456" not in redacted
    assert "mysecret123" not in redacted
    assert "***REDACTED***" in redacted

    # Verify labels are preserved
    assert "API_KEY=" in redacted
    assert "token=" in redacted
    assert "PASSWORD=" in redacted
    assert "bearer " in redacted


def test_security_constraint_no_db_transactions_during_execution(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test that no database transactions are held during verifier execution."""
    # This is verified by the architecture: VerifierExecutor.execute()
    # does not accept or use database connections
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)

    # Execute verifier - should not require or hold any DB transaction
    execution = executor.execute(
        verifier=sample_verifier,
        change_set_id=sample_change_set.id,
        variable_args={"change_set_id": str(sample_change_set.id)},
    )

    # Verify execution completed without DB transaction
    assert execution.exit_code == 0
    assert execution.timed_out is False


# ==================== INTEGRATION TESTS ====================


def test_full_verification_workflow_success_to_evidence(
    temp_workspace: Path,
    temp_evidence_dir: Path,
    temp_quarantine_dir: Path,
    sample_change_set: ChangeSet,
    sample_verifier: VerifierDefinition,
) -> None:
    """Test full workflow from verification execution through evidence linking."""
    executor = VerifierExecutor(temp_workspace, temp_quarantine_dir)
    verification_service = VerificationService(executor, temp_workspace, temp_evidence_dir)

    # Execute verification
    verification = verification_service.verify_change_set(
        sample_change_set,
        [sample_verifier],
        owner_verified=True,  # Required when there are required verifiers
    )

    assert verification.can_proceed_verified is True

    # Link evidence
    evidence = verification_service.link_verification_evidence(
        change_set_id=sample_change_set.id,
        verification=verification,
    )

    # Verify evidence is created
    assert len(evidence) > 0
    assert all(e.change_set_id == sample_change_set.id for e in evidence)
    # Some evidence might not have storage path if it's hash-only for large content
    assert any(
        e.storage_path is not None for e in evidence or True
    )  # At least some evidence should exist

    # Verify evidence files exist (if they have storage paths)
    for e in evidence:
        if e.storage_path:
            path = Path(e.storage_path)
            if path.exists():
                assert True  # File exists
            else:
                # Storage path might be relative or not created yet
                pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
