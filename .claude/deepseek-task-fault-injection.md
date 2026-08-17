# Task: Fault Injection Testing for Governed Executor (Task 15.8)

## Repository purpose

katsi is a local-first relational file-context engine with comprehensive workspace coordination,
governed execution, and recovery mechanisms. The governed executor orchestrates safe,
recoverable execution with fault injection testing capabilities.

## The unit of work

Add comprehensive fault injection tests for the governed executor that inject faults before
and after every journal, stage, replace, and step-record boundary. This is Task 15.8 from the
workspace coordination implementation plan.

## Context to understand

The governed executor (`packages/core/katsi_core/workspace/governed_executor.py`) has:

1. **FaultInjector class** with `maybe_fail(location)` method
2. **Multiple fault injection points** throughout execution:
   - Before/after lease acquisition
   - Before/after action journal recording
   - Before/after staging operations
   - Before/after file replacement
   - Before/after step recording
   - During recovery analysis
   - During rollback operations

3. **Recovery mechanisms** that must survive injected faults:
   - Action journal durability
   - Recovery blob store integrity
   - Exclusive lease cleanup
   - Staging area cleanup
   - Rollback capability

## Required behavior

You are to implement comprehensive fault injection tests that:

1. **Inject faults at every boundary point**:
   - Before lease acquisition
   - After lease acquisition but before journal recording
   - After journal recording but before staging
   - After staging but before file operations
   - After file operations but before step recording
   - During recovery analysis
   - During rollback execution

2. **Test recovery after each fault injection point**:
   - Action journal must be durable and replayable
   - Recovery blobs must be intact
   - Leases must be cleaned up or expirable
   - Staging files must not leak
   - Partial operations must be detectable
   - Rollback must complete successfully

3. **Verify system integrity after faults**:
   - No orphaned locks/leases
   - No leaked staging files
   - No corrupted action journals
   - No partial file operations persisted
   - Recovery state is accurate

## Contracts to preserve — do not change these

- The `GovernedExecutor` class interface and method signatures
- The `FaultInjector` class interface (`maybe_fail(location)`, `enable()`, `disable()`)
- Existing recovery mechanisms and services
- Action journal durability guarantees
- Recovery blob store integrity
- Lease service behavior
- Staging manager operations

## Allowed paths

- `tests/test_fault_injection_governed_executor.py` (create new test file)
- You may read but NOT modify:
  - `packages/core/katsi_core/workspace/governed_executor.py`
  - `packages/core/katsi_core/workspace/action_journal.py`
  - `packages/core/katsi_core/workspace/recovery_store.py`
  - `packages/core/katsi_core/workspace/exclusive_leases.py`

## Exclusions — do not touch

- Any production code in `packages/core/katsi_core/workspace/`
- Configuration files
- Other test files
- Database schemas or migrations

## Acceptance checks

Write these as pytest tests in `tests/test_fault_injection_governed_executor.py`:

1. **Fault injection at every boundary** (8-10 injection points):
   - Test each injection point independently
   - Verify fault is actually injected (exception raised)
   - Verify recovery mechanisms activate correctly

2. **Recovery verification after each fault**:
   - Action journal can be replayed successfully
   - Recovery blobs are accessible and intact
   - Leases are either released or expirable
   - Staging area is cleaned up
   - No partial operations are persisted

3. **System integrity verification**:
   - No orphaned resources after faults
   - Database state remains consistent
   - Filesystem state remains consistent
   - Recovery state accurately reflects partial execution

4. **Fault injection rate testing**:
   - Test with different failure rates (0.1, 0.5, 1.0)
   - Verify retry logic works correctly
   - Verify timeout mechanisms work correctly

5. **Concurrent fault testing**:
   - Multiple operations with faults
   - Verify no race conditions in recovery
   - Verify isolation between faulting operations

Run: `uv run pytest tests/test_fault_injection_governed_executor.py -v`

## Response format

Return a complete pytest test file with comprehensive fault injection tests.
Include test fixtures for creating test environments, mock services, and
verification helpers. Each test should be independent and cleanup properly.

Focus on:
- Complete boundary coverage (every journal/stage/replace/step boundary)
- Recovery verification after each fault type
- System integrity checks
- Clear test naming and documentation
- Proper cleanup and isolation between tests