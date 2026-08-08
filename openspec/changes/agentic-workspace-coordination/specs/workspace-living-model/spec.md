## Purpose

Define the persistent, provenance-backed workspace model that lets agents resume work and share trusted project context without replaying conversations or rescanning every file.

## ADDED Requirements

### Requirement: Workspace registration has a stable bounded identity
The system SHALL let the Workspace Owner register an Active Project Workspace with a stable identifier and one canonical local root. Active workspace roots MUST NOT overlap, and moving the root MUST NOT change the workspace identifier.

#### Scenario: Register a new workspace
- **WHEN** the owner registers a canonical directory that is not inside another active workspace
- **THEN** the system creates a stable workspace identifier and associates it with that root

#### Scenario: Reject an overlapping workspace
- **WHEN** the owner attempts to register a root that contains or is contained by another active workspace root
- **THEN** the system rejects registration and identifies the conflicting workspace

### Requirement: Workspace State survives clients and restarts
The system SHALL persist typed Workspace State independently of any MCP client session. The state SHALL include the active goal and intent version, Claims, verified decisions, open work, relationships, relevant changes, Work Leases, Change Set summaries, and action receipts.

#### Scenario: A later client resumes a workspace
- **WHEN** an agent opens a workspace after the producing client and Katsi process have exited
- **THEN** the agent can retrieve the previously committed Workspace State without reconstructing it from chat history

### Requirement: Conversation transcripts and hidden reasoning are excluded
The system MUST NOT require or retain full conversation transcripts, private chain-of-thought, or hidden model reasoning as Workspace State. Agents SHALL contribute typed Claims, decisions, open work, and action receipts instead.

#### Scenario: Agent publishes a discovery
- **WHEN** an agent wants a discovery to survive its session
- **THEN** the system accepts a typed Claim with provenance rather than requiring the conversation that produced it

### Requirement: Claims remain distinguishable from verified knowledge
Every Claim SHALL retain its author, evidence references, creation time, scope, confidence metadata, and verification status. Supported statuses SHALL include proposed, corroborated, verified, contradicted, and superseded. Model confidence or agreement among agents MUST NOT by itself produce verified status.

#### Scenario: Agent makes an unsupported assertion
- **WHEN** an agent publishes a Claim without deterministic or owner verification
- **THEN** the Claim remains proposed even when the agent reports high confidence

#### Scenario: Deterministic evidence verifies a Claim
- **WHEN** an approved deterministic check establishes the asserted condition and the evidence remains current
- **THEN** the system may mark the Claim verified and link the verification evidence

#### Scenario: Evidence changes after verification
- **WHEN** a verified Claim depends on a resource whose relevant content changes
- **THEN** the system invalidates the verification while preserving the Claim and its history

### Requirement: Workspace Briefs are compact and provenance-backed
The system SHALL produce a budget-bounded Workspace Brief containing the workspace goal, applicable verified and provisional Claims, decisions, relevant relationships, recent changes, active work, and unresolved questions. Each included item SHALL expose provenance and current verification state.

#### Scenario: Fresh agent requests a brief
- **WHEN** a fresh agent requests a Workspace Brief for a task and context budget
- **THEN** the system returns the most relevant durable state within the budget and identifies omitted or provisional context

#### Scenario: Brief contains invalidated context
- **WHEN** previously relevant context has been invalidated by an External Change
- **THEN** the brief labels it invalidated or excludes it rather than presenting it as current verified knowledge

### Requirement: Portable and private state remain separated
The system SHALL distinguish Portable Project State from Private Operational State. Owner-approved intent, invariants, verified decisions, and selected project metadata MAY be exported with the workspace. Agent credentials, Capability Grants, embeddings, caches, leases, detailed activity, and recovery material MUST remain private by default.

#### Scenario: Export portable state
- **WHEN** the owner exports a workspace's portable state
- **THEN** the export excludes agent credentials, capabilities, leases, embeddings, private activity, and recovery preimages

#### Scenario: Import portable state on another installation
- **WHEN** an owner imports valid portable state for the same workspace
- **THEN** the system restores the portable intent and decisions without restoring machine-specific authority

### Requirement: Evidence cannot grant authority
File content, retrieved chunks, web content, connected-source content, Claims, and model output SHALL be treated only as evidence. They MUST NOT activate intent, grant capabilities, authorize Change Sets, enable YOLO Mode, or create executable operations.

#### Scenario: Retrieved content contains instructions
- **WHEN** indexed or retrieved content instructs an agent to broaden access or execute an operation
- **THEN** the system retains the content as evidence but does not modify intent, authority, or executable state

