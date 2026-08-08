## Purpose

Define the typed, dependency-aware, authorized, recoverable, and verifiable workflow through which cooperating agents safely modify ordinary workspace files.

## ADDED Requirements

### Requirement: Submitted Change Sets are immutable and versioned
An agent SHALL submit a typed Change Set containing intent, workspace, affected resources, dependency set, expected hashes or absence assertions, preconditions, typed operations, postconditions, rollback behavior, and an idempotency key. After submission, the Change Set MUST be immutable; a revision SHALL create a linked successor version.

#### Scenario: Agent revises a submitted proposal
- **WHEN** an agent changes any submitted operation or dependency
- **THEN** the system creates a new Change Set version and preserves the superseded proposal

### Requirement: Change Sets follow explicit lifecycle states
The system SHALL preserve an append-only transition history through applicable states including proposed, validated, stale, rejected, authorized, applying, applied, verified, applied-unverified, rolling-back, rolled-back, and recovery-required. Invalid transitions MUST be rejected.

#### Scenario: Apply an unvalidated Change Set
- **WHEN** an agent requests application of a proposed but unvalidated Change Set
- **THEN** the system rejects the transition without modifying files

#### Scenario: Verification succeeds
- **WHEN** an applied Change Set passes every required deterministic verifier and invariant
- **THEN** the system records verified status and links the verification evidence

### Requirement: Validation is dependency-aware
Immediately before authorization and again before file replacement, the system SHALL compare every relevant dependency, invariant, intended output, and write target with the Change Set's expected state. Relevant differences SHALL mark the Change Set stale. Unrelated workspace events MUST NOT make it stale.

#### Scenario: Dependency changes after proposal
- **WHEN** a declared dependency has a different current hash from the Change Set expectation
- **THEN** validation marks the Change Set stale and identifies the changed dependency

#### Scenario: Unrelated file changes after proposal
- **WHEN** only resources outside the Change Set's dependency closure changed
- **THEN** validation may continue without rejecting the proposal as stale

### Requirement: Authorization cannot be self-expanded
The system SHALL evaluate an active Capability Grant and approval policy before application. A Change Set MUST NOT modify Agent Identities, Capability Grants, active intent, approval policy, YOLO Mode, workspace roots, or its own authorization.

#### Scenario: Agent proposes a capability grant for itself
- **WHEN** a Change Set contains an authority-plane mutation
- **THEN** the system rejects the Change Set regardless of the agent's existing file capabilities

### Requirement: The executor accepts only closed typed operations
The initial governed operation catalog SHALL be limited to creating a file, replacing a file with an exact expected hash, applying a deterministic patch to an exact expected hash, copying a file, moving a file within one workspace, creating a directory, moving a file into Recoverable Quarantine, restoring a quarantined file, and replacing a derived artifact. Paths SHALL be canonical and workspace-relative.

#### Scenario: Apply an allowed exact-hash replacement
- **WHEN** an authorized Change Set replaces a file whose current hash matches its expected hash
- **THEN** the executor may stage and apply the replacement under the Change Set safeguards

#### Scenario: Target path escapes the workspace
- **WHEN** an operation resolves outside the canonical workspace root through traversal or a symbolic link
- **THEN** the system rejects the entire Change Set before mutation

### Requirement: Dangerous and external operations are prohibited
Agent-generated Change Sets MUST NOT contain arbitrary shell commands, permanent deletion, cross-workspace writes, permission or ownership changes, symbolic-link creation, filesystem mounts, downloaded-binary execution, external network side effects, Git history rewriting, or authority-plane changes.

#### Scenario: Agent submits shell text as an operation
- **WHEN** a Change Set includes an operation outside the typed catalog
- **THEN** validation rejects it without executing the supplied content

#### Scenario: Agent requests permanent deletion
- **WHEN** a Change Set requests deletion instead of Recoverable Quarantine
- **THEN** validation rejects the operation

### Requirement: Application uses short exclusive resource leases
Exploration leases SHALL remain advisory. During validated application, the system SHALL acquire short exclusive leases over the Change Set write set, release them after the terminal outcome, and refuse application when a conflicting exclusive lease exists.

#### Scenario: Two Change Sets write the same file
- **WHEN** one Change Set holds the exclusive application lease for a file
- **THEN** a second overlapping Change Set cannot begin application

### Requirement: Governed mutation is journaled and recoverable
Before modifying any file, the system SHALL durably record the intended operations, current hashes, recoverable preimages, and recovery plan in an append-only Action Journal. Every operation SHALL be idempotent. Failure after partial application SHALL trigger compensation or recovery-required status.

#### Scenario: Failure occurs after the first of several writes
- **WHEN** a later operation or required postcondition fails
- **THEN** the system restores recorded preimages when safe and records the rollback outcome

#### Scenario: The same application request is retried
- **WHEN** a client repeats an application request with the same idempotency key
- **THEN** the system returns or resumes the existing execution instead of applying the mutations twice

### Requirement: Results are staged before replacement
The system SHALL compute and stage resulting file bytes before replacing current files. It SHALL use atomic per-file replacement where the platform supports it, but MUST NOT claim multi-file ACID atomicity.

#### Scenario: Staging fails
- **WHEN** any output cannot be fully staged before application begins
- **THEN** the system performs no target-file replacements and records the failed attempt

### Requirement: Verification is deterministic or owner-confirmed
An applied Change Set SHALL become verified only when all configured deterministic checks and Executable Invariants pass or the Workspace Owner explicitly verifies it. Agent reports, prose review, model confidence, and agreement among models MUST NOT establish verified status. If no applicable verifier exists, the result SHALL be applied-unverified.

#### Scenario: Agent reports success without a verifier
- **WHEN** application completes but no approved deterministic verifier applies
- **THEN** the system records applied-unverified rather than verified

#### Scenario: Required verifier fails
- **WHEN** an approved verifier or invariant fails after application
- **THEN** the system begins rollback or records recovery-required when safe automatic rollback is impossible

### Requirement: Verification commands are owner-configured
The system SHALL allow only verifiers selected from an owner-configured catalog. Agents MUST NOT supply arbitrary command strings as verifiers, though they MAY select an allowed verifier and arguments permitted by its definition.

#### Scenario: Agent selects a configured test verifier
- **WHEN** a Change Set requests an applicable verifier from the workspace catalog
- **THEN** the system runs it under its configured scope, timeout, environment, and argument policy

#### Scenario: Agent supplies an unregistered command
- **WHEN** a Change Set contains a verifier command absent from the owner catalog
- **THEN** validation rejects the verifier

### Requirement: Incomplete executions recover after restart
On startup, the system SHALL inspect nonterminal Action Journal entries before accepting overlapping governed mutations. It SHALL safely resume, compensate, or mark recovery-required with an owner-visible explanation.

#### Scenario: Process stops during application
- **WHEN** Katsi restarts with a Change Set left in applying or rolling-back state
- **THEN** it performs recovery analysis before allowing another Change Set to modify the affected resources

### Requirement: YOLO Mode changes approval only
When enabled by the Workspace Owner, YOLO Mode SHALL be scoped to an Agent Identity, workspace, and action classes. It MAY remove per-action owner approval but MUST NOT bypass capabilities, dependency validation, exclusive application leases, operation allowlists, invariants, verification, journaling, or recovery. Initial YOLO policy MUST NOT allow modification of owner-authored originals.

#### Scenario: YOLO Change Set is within scope
- **WHEN** an enabled identity proposes an allowed derived-artifact operation within its YOLO scope
- **THEN** the system may authorize it without per-action approval while applying every other safeguard

#### Scenario: YOLO agent attempts to modify an original
- **WHEN** an initial YOLO policy receives a Change Set that patches or replaces an owner-authored original
- **THEN** the system requires explicit owner approval or rejects the operation

