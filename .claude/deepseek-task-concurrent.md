# Task: Concurrent Change Coverage Testing (Tasks 19.3-19.4)

## Repository purpose

katsi is a local-first relational file-context engine with comprehensive workspace coordination,
governed execution, and concurrent change management. The system handles multiple agents
working simultaneously with proper conflict detection and resolution.

## The unit of work

Add Agent C concurrent relevant-change coverage proving stale proposals are blocked with exact
invalidation evidence, and unrelated concurrent-change coverage proving independent Change Sets
remain valid. These are Tasks 19.3 and 19.4 from the workspace coordination implementation plan.

## Context to understand

Concurrent change management involves:

1. **Change Set proposal lifecycle**:
   - Proposed with exact dependencies (resource versions, hashes, invariants)
   - Validated against current workspace state
   - Can become stale if relevant changes occur
   - Can be superseded by newer proposals
   - Can be approved or rejected by owners

2. **Concurrent modification scenarios**:
   - **Agent C relevant change**: Agent C modifies files that Agent B's proposal depends on
   - **Unrelated concurrent changes**: Agent C modifies different files than Agent B's proposal
   - **Stale proposals**: Proposals that depend on outdated resource versions
   - **Invalidation evidence**: Exact events that caused a proposal to become stale

3. **Conflict detection**:
   - Exact resource version dependencies
   - Hash-based content validation
   - Invariant version dependencies
   - Absence assertions (files should not exist)
   - Target hash expectations

## Required behavior

You are to implement comprehensive concurrent change tests that:

1. **Test Agent C relevant-change blocking**:
   - Agent B creates proposal depending on file v1
   - Agent C modifies file to v2 (relevant change)
   - Agent B's proposal becomes stale
   - Agent B's proposal is blocked
   - Exact invalidation evidence is returned (which file changed, from v1 to v2)

2. **Test unrelated concurrent changes**:
   - Agent B creates proposal depending on file A
   - Agent C modifies file B (unrelated change)
   - Agent B's proposal remains valid
   - Agent B's proposal can proceed
   - No conflict between independent changes

3. **Test various dependency scenarios**:
   - Exact hash dependencies
   - Resource version dependencies
   - Invariant dependencies
   - Absence assertions (file should not exist)
   - Multiple file dependencies

4. **Test invalidation evidence quality**:
   - Exact resource that changed
   - Version transitions (v1 → v2)
   - Hash mismatches
   - Invariant violations
   - Clear reason for invalidation

5. **Test race condition handling**:
   - Proposals submitted simultaneously
   - Changes during validation
   - Changes during approval
   - Proper serialization when needed

## Contracts to preserve — do not change these

- Change Set proposal lifecycle
- Dependency validation logic
- Conflict detection mechanisms
- Invalidation evidence generation
- Approval/denial workflows

## Allowed paths

- `tests/test_concurrent_changes.py` (create new test file)
- You may read but NOT modify:
  - `packages/core/katsi_core/workspace/change_sets.py` (if exists)
  - `packages/core/katsi_core/workspace/dependencies.py` (if exists)
  - Related Change Set contracts

## Exclusions — do not touch

- Any production code in `packages/core/katsi_core/workspace/`
- Change Set validation logic
- Conflict detection algorithms
- Database schemas or migrations

## Acceptance checks

Write these as pytest tests in `tests/test_concurrent_changes.py`:

1. **Agent C relevant-change tests** (Task 19.3):
   - Agent B proposal, Agent C relevant modification, proposal blocked
   - Exact invalidation evidence returned
   - Specific file and version change identified
   - Clear reason communicated
   - Proposal cannot proceed with stale dependencies

2. **Unrelated concurrent-change tests** (Task 19.4):
   - Agent B proposal, Agent C unrelated modification, proposal valid
   - Independent Change Sets proceed in parallel
   - No false conflicts between unrelated changes
   - Proper isolation of independent work
   - Both proposals can succeed

3. **Dependency scenario tests**:
   - Hash dependency conflicts
   - Resource version conflicts
   - Invariant violations
   - Absence assertion failures
   - Multiple dependency combinations

4. **Invalidation evidence tests**:
   - Exact file that caused conflict
   - Version before and after
   - Hash differences
   - Invariant specifics
   - Actionable error messages

5. **Race condition tests**:
   - Simultaneous proposal submissions
   - Changes during validation window
   - Changes during approval window
   - Proper serialization when conflicts occur
   - No lost updates or silent overwrites

Run: `uv run pytest tests/test_concurrent_changes.py -v`

## Response format

Return a complete pytest test file with comprehensive concurrent change testing.
Include test fixtures for multi-agent setup, change proposal creation, concurrent
modification simulation, and invalidation evidence verification. Focus on realistic
concurrent work scenarios and proper conflict detection.