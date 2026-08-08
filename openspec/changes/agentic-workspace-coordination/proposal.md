## Why

Files persist while agent understanding, decisions, and coordination disappear between sessions. Katsi's current relational retrieval engine can recover file context, but it does not yet provide the transactional workspace state required for multiple local agents to share verified knowledge, coordinate concurrent work, or prevent stale and unsafe changes.

## What Changes

- Add a persistent Living Model that retains typed, provenance-backed workspace state without storing conversation transcripts or hidden reasoning.
- Add owner-registered Agent Identities, scoped Capability Grants, Claims, Workspace Briefs, and advisory Work Leases for local multi-agent continuity and coordination.
- Reconcile file creation, modification, movement, and deletion into the Living Model and invalidate only state that depends on changed resources.
- Add immutable, typed Change Sets with dependency hashes, validation, authorization, a closed operation catalog, deterministic verification, journaling, and recovery.
- Expose the coordination and governed-action lifecycle through MCP while retaining the existing retrieval tools as lower-level primitives.
- Keep filesystem bytes authoritative, ordinary direct writes possible, and all control-plane operations local.

## Capabilities

### New Capabilities

- `workspace-living-model`: Persistent workspace state, provenance, Claims, Workspace Briefs, and portable/private state boundaries.
- `agent-coordination`: Agent Identities, Capability Grants, Work Leases, concurrent-agent visibility, and scoped authority.
- `filesystem-reconciliation`: Observation and reconciliation of external filesystem changes, content identity, history, and dependency invalidation.
- `governed-change-sets`: Typed Change Set lifecycle, allowed operations, stale validation, authorization, verification, journaling, rollback, and recovery.

### Modified Capabilities

None.

## Impact

- `katsi_core` gains an authoritative transactional workspace-state boundary while Kùzu and LanceDB become rebuildable graph and retrieval projections.
- The MCP package gains workspace, coordination, Claim, lease, and Change Set operations while preserving existing retrieval tools.
- The desktop application evolves toward an owner-facing workspace control center for intent, identities, capabilities, active work, proposed changes, verification, and recovery.
- Local storage gains durable operational state and an append-only action history; portable owner-approved project state remains separate.
- The initial deployment remains one owner on one machine with multiple cooperating MCP clients and agents.
