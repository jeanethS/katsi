# Task: YOLO Authorization Boundary Testing (Task 18.6)

## Repository purpose

katsi is a local-first relational file-context engine with comprehensive workspace coordination.
YOLO (You Only Live Once) authorization mode allows auto-authorization for certain operations
while maintaining safety boundaries.

## The unit of work

Add comprehensive tests proving YOLO cannot grant authority, expand scope, bypass safeguards,
modify prohibited originals, or permanently delete data. This is Task 18.6 from the workspace
coordination implementation plan.

## Context to understand

YOLO authorization mode involves:

1. **YOLO activation and revocation**:
   - Scoped to Agent Identity, workspace, operation classes
   - Limits and policy version constraints
   - Owner activation and revocation control

2. **Initial YOLO policy restrictions**:
   - Only allowed derived artifacts
   - Reversible organization operations
   - Requires owner approval for owner-authored original modification
   - Cannot grant authority itself

3. **YOLO execution path**:
   - Routes through identical validation, lease, operation, journal, verification, rollback services
   - Auto-authorization for allowed operations
   - Automatic suspension after authorization mismatch, invariant failure, verification failure, or recovery-required outcome

4. **Safety boundaries**:
   - No permanent deletion
   - No authority expansion
   - No safeguard bypass
   - No prohibited original modifications

## Required behavior

You are to implement comprehensive YOLO boundary tests that:

1. **Test authority restrictions**:
   - YOLO cannot grant additional authority
   - YOLO cannot expand its own scope
   - YOLO cannot bypass authorization checks
   - YOLO cannot modify policy restrictions

2. **Test scope restrictions**:
   - YOLO cannot expand beyond allowed operation classes
   - YOLO cannot modify prohibited originals (owner-authored content)
   - YOLO cannot perform operations outside defined scope
   - YOLO cannot exceed configured limits

3. **Test safeguard preservation**:
   - YOLO cannot bypass validation checks
   - YOLO cannot bypass verification requirements
   - YOLO cannot bypass rollback mechanisms
   - YOLO cannot bypass recovery requirements

4. **Test data protection**:
   - YOLO cannot permanently delete data
   - YOLO cannot bypass quarantine mechanisms
   - YOLO cannot disable logging/auditing
   - YOLO cannot hide its actions

5. **Test automatic suspension**:
   - Suspension after authorization mismatch
   - Suspension after invariant failure
   - Suspension after verification failure
   - Suspension after recovery-required outcome
   - Cannot reactivate without owner approval

## Contracts to preserve — do not change these

- YOLO activation/revocation interface
- YOLO policy validation logic
- Authorization service interface
- Change Set validation workflows
- Existing safety mechanisms

## Allowed paths

- `tests/test_yolo_boundaries.py` (create new test file)
- You may read but NOT modify:
  - `packages/core/katsi_core/workspace/yolo.py` (if exists)
  - `packages/core/katsi_core/workspace/authorization.py`
  - Related YOLO contracts

## Exclusions — do not touch

- Any production code in `packages/core/katsi_core/workspace/`
- YOLO activation/revocation mechanisms
- Authorization service logic
- Database schemas or migrations

## Acceptance checks

Write these as pytest tests in `tests/test_yolo_boundaries.py`:

1. **Authority restriction tests**:
   - Attempt to grant additional authority fails
   - Attempt to expand scope fails
   - Attempt to modify policy fails
   - Attempt to bypass authorization fails

2. **Scope restriction tests**:
   - Cannot perform operations outside allowed classes
   - Cannot modify owner-authored originals
   - Cannot exceed operation limits
   - Cannot expand workspace boundaries

3. **Safeguard preservation tests**:
   - Validation still enforced
   - Verification still required
   - Rollback still functional
   - Recovery still operational

4. **Data protection tests**:
   - Permanent deletion blocked
   - Quarantine mechanisms active
   - Logging/auditing maintained
   - Actions are traceable

5. **Automatic suspension tests**:
   - Suspends on authorization mismatch
   - Suspends on invariant failure
   - Suspends on verification failure
   - Suspends on recovery-required outcome
   - Requires owner approval to reactivate

Run: `uv run pytest tests/test_yolo_boundaries.py -v`

## Response format

Return a complete pytest test file with comprehensive YOLO boundary testing.
Include test fixtures for YOLO activation, various operation scenarios, and
boundary verification. Focus on proving what YOLO CANNOT do rather than what it can.