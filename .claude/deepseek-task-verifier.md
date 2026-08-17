# Task: Verifier Testing (Task 16.8)

## Repository purpose

katsi is a local-first relational file-context engine with comprehensive workspace coordination,
governed execution, and verification mechanisms. The verification system runs owner-configured
verifiers on Change Sets before they can be applied.

## The unit of work

Add comprehensive tests for verifier success/failure/timeout scenarios, owner verification,
interrupted rollback, corrupted preimage handling, and restart recovery. This is Task 16.8 from
the workspace coordination implementation plan.

## Context to understand

The verification system involves:

1. **Verifier definitions** configured by owners with:
   - Executable/argument prefix
   - Allowed variable arguments
   - CWD scope restrictions
   - Environment variable allowlist
   - Timeout and output limits
   - Applicability rules
   - Required policy mode

2. **Verifier execution** with security constraints:
   - `shell=False` (no command injection)
   - Bounded output capture
   - Secret redaction
   - No database transaction during execution
   - Input/resource version rechecking before committing

3. **Verification outcomes**:
   - Success → Change Set can proceed
   - Failure → Change Set blocked
   - Timeout → handled as failure
   - Owner verification → manual approval path

4. **Recovery scenarios**:
   - Interrupted verification (system crash during verifier run)
   - Corrupted preimages (backup files damaged)
   - Restart recovery (system restart during verification)

## Required behavior

You are to implement comprehensive verifier tests that:

1. **Test verifier execution scenarios**:
   - Successful verification with correct output
   - Verification failure (exit code != 0)
   - Timeout scenarios (verifier runs too long)
   - Output limit scenarios (verifier produces too much output)
   - Invalid verifier configuration

2. **Test owner verification workflow**:
   - Manual owner approval after verifier passes
   - Owner rejection despite verifier success
   - Owner override of verifier failure (if allowed by policy)

3. **Test interrupted verification recovery**:
   - System crash during verifier execution
   - Verifier process killed mid-execution
   - Database transaction rollback during verification
   - Recovery blob corruption scenarios

4. **Test corrupted preimage handling**:
   - Backup files missing or corrupted
   - Recovery blob integrity failures
   - Graceful degradation when preimages unavailable

5. **Test restart recovery**:
   - System restart during active verification
   - Verification state restoration after restart
   - Cleanup of incomplete verification attempts

## Contracts to preserve — do not change these

- Verifier configuration schema and validation
- Verifier execution security constraints (shell=False, bounded output, etc.)
- Owner approval/denial workflows
- Recovery blob store interface
- Action journal recovery mechanisms

## Allowed paths

- `tests/test_verifier_scenarios.py` (create new test file)
- You may read but NOT modify:
  - `packages/core/katsi_core/workspace/verifier.py` (if exists)
  - `packages/core/katsi_core/workspace/governed_executor.py`
  - Related verification contracts

## Exclusions — do not touch

- Any production code in `packages/core/katsi_core/workspace/`
- Verifier execution security mechanisms
- Database schemas or migrations
- Configuration files

## Acceptance checks

Write these as pytest tests in `tests/test_verifier_scenarios.py`:

1. **Verifier execution tests**:
   - Success scenario (verifier returns 0)
   - Failure scenario (verifier returns non-zero)
   - Timeout scenario (verifier exceeds time limit)
   - Output limit scenario (verifier exceeds output limit)
   - Invalid configuration (bad executable, missing arguments)

2. **Owner verification tests**:
   - Owner approval after verifier success
   - Owner rejection despite verifier success
   - Policy-based verification requirements
   - Owner override capabilities

3. **Interrupted execution tests**:
   - System crash during verifier run
   - Process termination mid-execution
   - Database connection loss during verification
   - Filesystem corruption during verification

4. **Corrupted preimage tests**:
   - Missing backup files
   - Corrupted recovery blobs
   - Graceful handling of unavailable preimages
   - Fallback behavior when integrity checks fail

5. **Restart recovery tests**:
   - Active verification at system restart
   - Verification state restoration
   - Cleanup of incomplete attempts
   - Resume capability after restart

Run: `uv run pytest tests/test_verifier_scenarios.py -v`

## Response format

Return a complete pytest test file with comprehensive verifier testing.
Include test fixtures for creating mock verifiers, simulating various scenarios,
and verification helpers. Focus on realistic failure scenarios and recovery testing.