# Katsi: Agentic Filesystem Vision

**Status:** Accepted  
**Date:** 2026-08-06

## Product thesis

> **Katsi provides persistent local workspace memory and coordination for AI agents.**

Katsi is an agentic layer over existing filesystems. It does not replace the
filesystem that stores bytes. It maintains a persistent, relational model through
which privately owned agents retain context, coordinate with one another, and take
governed action over long-lived project folders.

The current relational retrieval engine is the wedge. The destination is a local
workspace control plane for agents.

## The problem

Files persist, but agent understanding does not.

Each new agent session enters a project as a stateless visitor. It repeatedly reads
the same files, reconstructs decisions already made, and produces conclusions that
disappear with the conversation. When several agents use the same folder, they lack
shared knowledge of active work and can propose changes against stale assumptions.
When they mutate files directly, they often have no durable account of intent,
dependencies, verification, or recovery.

This appears as four failures, but they share one cause:

1. **Amnesia:** understanding and decisions disappear between sessions.
2. **Repeated exploration:** every agent spends time and tokens rediscovering the
   same project.
3. **Coordination failure:** agents do not know what other agents learned, decided,
   or are changing.
4. **Unsafe action:** agents modify related files without a trusted model of intent,
   dependencies, concurrent changes, or successful outcomes.

The missing abstraction is a persistent, agent-readable workspace model.

## First user and deployment boundary

The first user is an individual developer or technical power user running multiple
local or client-hosted agents over long-lived project folders.

The initial deployment boundary is deliberately narrow:

- one Workspace Owner;
- one machine;
- multiple agents and MCP clients;
- ordinary local project folders;
- local observation, planning, policy evaluation, and verification.

Cross-device synchronization, team authority, and cloud-hosted coordination are
later problems. They must not complicate the first trust model.

## What Katsi owns

The existing filesystem remains authoritative for file bytes. Katsi owns:

- the Living Model of the workspace;
- typed, provenance-backed Workspace State;
- user-approved intent and executable invariants;
- agent identities and scoped capabilities;
- Claims, decisions, open work, and Work Leases;
- proposed and executed Change Sets;
- verification, recovery, and action history.

Katsi observes direct filesystem writes as External Changes. It provides stronger
guarantees only for cooperating agents using its Governed Path. It is not an
operating-system security sandbox and does not claim to control arbitrary processes
with direct filesystem access.

## From retrieval to a Living Model

Today, Katsi provides local summarize-once ingestion, vector and graph retrieval,
and budgeted context bundles. Those remain core primitives, but they are not the
complete product.

The Living Model adds durable state that cannot be reconstructed reliably from file
similarity alone:

- the active project goal and Intent Snapshot;
- verified Claims and their evidence;
- architectural and owner decisions;
- unresolved questions, blockers, and open work;
- file relationships and dependencies;
- recent changes and invalidated context;
- active Agent Identities and Work Leases;
- proposed, applied, verified, failed, and rolled-back Change Sets;
- action receipts that future agents can inspect.

Katsi retains typed workspace state, not full chat transcripts or hidden model
reasoning. Agents contribute inspectable Claims rather than writing unqualified
facts into shared memory.

## Core interaction

A cooperating agent follows this lifecycle:

1. **Open:** identify itself and open an Active Project Workspace.
2. **Orient:** receive a compact Workspace Brief containing the goal, relevant
   verified Claims, decisions, relationships, recent changes, active work, and open
   questions.
3. **Coordinate:** acquire an advisory, time-bounded Work Lease over a declared
   scope.
4. **Learn:** reuse relational context and publish new Claims with evidence,
   provenance, scope, confidence, and verification status.
5. **Propose:** submit a typed Change Set containing intent, affected resources,
   dependency set, expected content hashes, preconditions, operations,
   postconditions, and rollback behavior.
6. **Validate:** detect whether any relevant input, invariant, dependency, or
   intended output changed since the proposal was built.
7. **Authorize:** apply the Workspace Owner's Capability Grants and current
   approval mode.
8. **Execute:** briefly make the lease exclusive over affected resources, journal
   recoverable preimages, and apply allowlisted operations.
9. **Verify:** run deterministic local checks and executable invariants.
10. **Remember:** record the result, release work, and make verified outcomes
    available to future agents.

Unrelated workspace changes do not invalidate a Change Set. Relevant concurrent
changes do.

## Claims and truth

An agent cannot promote its own conclusion to fact merely by expressing high
confidence. Claims move through explicit states such as:

- proposed;
- corroborated;
- verified;
- contradicted;
- superseded.

Model confidence is metadata, not verification. A Claim becomes verified only
through deterministic evidence, a configured check, direct authoritative evidence,
or the Workspace Owner.

Files, messages, connected-source data, and web content are evidence only. They can
never become intent, policy, authority, or executable operations. This is the hard
boundary that prevents retrieved prompt injection from expanding an agent's
capabilities.

## Intent and authority

The Workspace Owner expresses Declared Intent in natural language. Katsi compiles
it locally into a versioned, inspectable Intent Snapshot containing goals,
preferences, executable invariants, ambiguities, and required capabilities.

The first Intent Snapshot and every Intent Amendment require owner activation.
Katsi may propose an amendment but cannot activate it, weaken an invariant, broaden
its workspace scope, or grant itself new authority.

Each agent operates under a durable Agent Identity registered by the Workspace
Owner. Capability Grants are revocable and scoped to an identity, a workspace, and
specified operation classes. Client and model names are descriptive metadata, not
authority.

## Governed action and recovery

A Change Set is verified only when its configured deterministic checks and
Executable Invariants pass or the Workspace Owner explicitly verifies it. An agent
reporting success is insufficient.

If no applicable verifier exists, the honest outcome is `Applied Unverified`. Katsi
must never convert uncertainty into confident prose and call it success.

Before a governed mutation, Katsi records affected content hashes and recoverable
preimages in an append-only Action Journal. Failed postconditions trigger rollback.
Autonomous operation may use Recoverable Quarantine but may not permanently destroy
originals or action history.

## Autonomy ladder

Katsi earns autonomy in four levels:

### Level 1 — Durable coordination

- Agent Identities and scoped capabilities;
- Workspace Briefs;
- Claims, decisions, open work, and action receipts;
- advisory Work Leases;
- continuous model updates from observed filesystem changes.

This level addresses amnesia, repeated exploration, and basic coordination without
autonomous mutation.

### Level 2 — Change validation

- typed Change Sets;
- dependency sets and expected hashes;
- detection of relevant concurrent changes;
- stale-plan rejection;
- owner review and approval.

### Level 3 — Governed execution

- allowlisted file operations;
- short exclusive application leases;
- deterministic verification;
- append-only journaling;
- automatic rollback and recovery;
- auditable results.

### Level 4 — YOLO Mode

YOLO Mode removes per-action human approval for a specific Agent Identity and
workspace. It does not remove capability boundaries, invariants, stale-change
validation, verification, journaling, or recovery. It ships only after governed
execution has demonstrated reliability.

## Product surfaces

### MCP workspace protocol

Katsi remains MCP-first and keeps files ordinary. Its existing retrieval tools stay
available as low-level primitives. The expanded protocol supports these conceptual
operations:

- open and inspect a workspace;
- obtain a Workspace Brief;
- acquire, renew, and release work;
- publish and inspect Claims;
- inspect decisions and open work;
- propose and validate Change Sets;
- approve, apply, verify, and recover governed changes;
- inspect action history and current capabilities.

Exact tool names and contracts belong in a separate implementation specification.

### Workspace Control Center

The desktop application becomes the Workspace Owner's control surface for:

- active intent and proposed amendments;
- Agent Identities and Capability Grants;
- active work and leases;
- Claims, contradictions, and unresolved questions;
- recent External Changes;
- proposed and executed Change Sets;
- verification and recovery history;
- Governed Agency and YOLO status.

Search and question answering remain useful, but they are utilities rather than the
center of the product.

## State boundary

Katsi separates two kinds of durable state.

**Portable Project State** may travel with the workspace:

- owner-approved intent;
- executable invariants;
- verified decisions;
- selected project metadata needed for continuity.

**Private Operational State** remains machine-local by default:

- embeddings and retrieval caches;
- Agent Identities and Capability Grants;
- Work Leases;
- detailed activity;
- recovery data and private action history.

This boundary makes projects portable without leaking machine-specific authority or
every agent action into the repository.

## Flagship dogfood demonstration

Katsi demonstrates the product on its own repository:

1. Agent A explores Katsi and publishes verified architectural Claims, decisions,
   relationships, and open work.
2. Agent B starts later from another MCP client and receives a compact Workspace
   Brief without rescanning the repository.
3. Agent B acquires work and proposes a multi-file Change Set.
4. Agent C concurrently changes a relevant dependency.
5. Katsi invalidates only the affected context and blocks the stale Change Set.
6. Agent B refreshes its brief, revises the proposal, applies it through the
   Governed Path, and verifies the result.
7. The outcome and its evidence become durable context for the next agent.

This demonstrates all four product failures in one loop: memory survives,
exploration is reused, agents coordinate, and unsafe stale action is prevented.

## Success metric

The headline metric is **Time to Verified Action**: elapsed time and context cost
from a fresh agent session to a successfully verified Change Set.

Supporting measures include:

- exploration tokens avoided on repeated sessions;
- unchanged content never re-summarized;
- relevant stale Change Sets prevented;
- unrelated parallel work allowed to proceed;
- Claims reused with valid provenance;
- governed mutations with complete verification and recovery evidence;
- rollback success after failed postconditions.

## Implementation sequence

### Foundation — Trustworthy Living Model

Strengthen the current retrieval wedge so it can maintain current state rather than
only index snapshots:

- observe creates, modifications, moves, and deletions;
- remove stale vectors and graph relationships;
- preserve summarize-once behavior by content hash;
- invalidate only affected context;
- expose provenance and change history.

### Release 1 — Durable coordination

Add workspaces, owner-registered Agent Identities, Workspace State, Claims,
decisions, open work, Workspace Briefs, action receipts, and advisory Work Leases.
No autonomous writes are required for this release.

### Release 2 — Validated Change Sets

Add typed proposals, dependency tracking, expected hashes, stale detection,
capability evaluation, and owner approval. Agents may still apply approved changes
using their existing tools while Katsi records and verifies the result.

### Release 3 — Governed executor

Add allowlisted file operations, short exclusive application leases, Action
Journal preimages, deterministic verification, rollback, and recovery inspection.

### Release 4 — Constrained autonomy

Enable YOLO Mode only for explicit identities, workspaces, and action classes after
the same governed executor has earned trust.

## Explicit non-goals for the initial product

- implementing a kernel or mounted virtual filesystem;
- replacing Git or ordinary project files;
- controlling non-cooperating processes with direct filesystem access;
- storing full conversations or hidden model reasoning;
- treating model agreement as factual verification;
- arbitrary shell execution as a Change Set operation;
- permanent autonomous deletion;
- global YOLO Mode;
- team, cross-device, or cloud-hosted authority;
- making a vertical demo scenario the identity of the product.

## Relationship to existing Katsi work

The existing MCP retrieval server is not discarded. Summarize-once ingestion,
relational retrieval, evidence scoring, and context bundles become the perception
layer of the Living Model.

The previously explored Sentinel and travel-planning scenarios may still be useful
demonstrations of reconciliation, but neither defines Katsi's product category.
The canonical dogfood scenario is multi-agent continuity and safe coordination over
Katsi's own repository.

## Decision record

Canonical language lives in [`CONTEXT.md`](../CONTEXT.md). Architectural rationale
lives in [`docs/adr/`](./adr/), including the local control loop, evidence-authority
separation, filesystem layering, versioned Change Sets, MCP-first surface,
owner-registered identities, state separation, and deterministic verification with
recovery.
