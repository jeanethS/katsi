## Purpose

Define how locally owned agents establish durable identity, receive scoped authority, advertise active work, and coordinate safely across MCP clients.

## ADDED Requirements

### Requirement: The owner registers every Agent Identity
The system SHALL require the Workspace Owner to register an Agent Identity before an agent can publish durable state or use the Governed Path. A client name, model name, process identifier, or agent-supplied label MUST NOT establish identity or authority.

#### Scenario: Registered agent authenticates
- **WHEN** an MCP client presents a valid private credential for an active Agent Identity
- **THEN** the system attributes authorized operations to that identity

#### Scenario: Client self-declares an identity
- **WHEN** an MCP client supplies only an unregistered agent or application name
- **THEN** the system denies durable coordination and governed-action operations

### Requirement: Agent credentials remain private and revocable
The system SHALL store only a non-reversible representation of an agent credential, SHALL NOT include credentials in portable state or logs, and SHALL let the owner revoke an identity immediately.

#### Scenario: Owner revokes an identity
- **WHEN** the owner revokes an Agent Identity
- **THEN** new operations using that identity are denied and its active Work Leases cease to authorize exclusivity

### Requirement: Capabilities are explicit and workspace-scoped
Every durable or governed operation SHALL require a Capability Grant issued by the Workspace Owner to an Agent Identity for a specific workspace and operation class. The system MUST deny operations outside the granted workspace, scope, risk limit, or action class.

#### Scenario: Agent operates within a grant
- **WHEN** an agent requests an operation covered by an active Capability Grant
- **THEN** the system evaluates the operation using that grant and records the grant used

#### Scenario: Agent attempts to expand its scope
- **WHEN** an agent requests an operation outside its Capability Grant or proposes a Change Set that grants new authority
- **THEN** the system rejects the request and leaves existing authority unchanged

### Requirement: Work Leases coordinate active work
An authorized agent SHALL be able to acquire, renew, and release a time-bounded Work Lease describing its task and resource scope. Exploration leases SHALL be advisory and visible to other agents. Expired or released leases SHALL NOT continue to reserve work.

#### Scenario: Agent acquires advisory work
- **WHEN** an authorized agent acquires an available exploration scope
- **THEN** the system records the lease, expiry, task, and scope and exposes them to other agents

#### Scenario: Another agent inspects overlapping work
- **WHEN** a second agent opens a workspace with an active overlapping advisory lease
- **THEN** the Workspace Brief identifies the overlap without blocking the second agent from exploring

#### Scenario: Agent disappears
- **WHEN** a lease reaches its expiry without renewal
- **THEN** the system marks it expired and no longer represents the work as actively reserved

### Requirement: Durable contributions are attributable
Every Claim, lease transition, proposed Change Set, and agent-authored open-work update SHALL retain the responsible Agent Identity and time. Historical attribution MUST survive identity revocation.

#### Scenario: Inspect a revoked agent's contribution
- **WHEN** the owner inspects a Claim created by a later-revoked identity
- **THEN** the system preserves the original attribution while showing that the identity is revoked

### Requirement: Owner-only authority transitions are isolated
Registering identities, granting or revoking capabilities, activating an Intent Snapshot, and enabling or disabling YOLO Mode SHALL require owner authority and SHALL be committed separately from agent-authored workspace or filesystem mutations.

#### Scenario: Change Set includes an authority change
- **WHEN** an agent proposes a Change Set containing an identity, capability, intent-activation, or YOLO transition
- **THEN** the system rejects the Change Set instead of combining authority expansion with the requested work

### Requirement: Initial coordination is single-owner and single-machine
The system SHALL support multiple local MCP clients and Agent Identities under one Workspace Owner on one machine. It MUST NOT imply team, cross-device, or cloud-hosted authority semantics in this capability.

#### Scenario: Two local clients coordinate
- **WHEN** two authenticated local clients operate on the same workspace
- **THEN** both observe committed Claims, decisions, leases, and relevant events according to their capabilities

