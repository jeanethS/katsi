# Katsi

Katsi provides persistent local workspace memory and coordination for AI agents. It is an agentic layer over existing filesystems through which privately owned agents retain context, coordinate, reconcile projects against user-approved intent, and act safely.

## Language

**Unsafe Action**:
A change to a file-backed system made without understanding its dependencies, applying its authorization policy, or verifying its outcome.
_Avoid_: Blind mutation, unguarded edit

**Living Model**:
A persistent, shared, and continuously maintained representation of a workspace's current state, history, dependencies, invariants, provenance, and relevant agent activity.
_Avoid_: Index, cache, snapshot

**Workspace State**:
Typed, provenance-backed knowledge retained across agent sessions, including goals, decisions, Claims, open work, relationships, changes, and action receipts, but excluding conversation transcripts and hidden reasoning.
_Avoid_: Chat history, memory dump, chain of thought

**Governed Agency**:
The default operating mode in which Katsi may execute typed actions only after applicable policies and approval requirements are satisfied, then verifies and records the outcome.
_Avoid_: Copilot mode, manual mode

**YOLO Mode**:
An explicitly enabled operating mode in which Katsi detects, plans, executes, and verifies corrective actions without per-action human approval.
_Avoid_: Governed agency, advisory mode

**Personal Agent**:
An AI agent operating privately on one person's machine and files, potentially alongside other agents owned by that person.
_Avoid_: Lifestyle assistant, consumer concierge, cloud agent

**Workspace Owner**:
The individual developer or technical power user who controls an Active Project Workspace on one machine and authorizes Personal Agents and MCP clients to operate within it.
_Avoid_: Administrator, customer, team owner

**Agentic Filesystem**:
A local semantic, coordination, and policy layer over an existing filesystem that gives Personal Agents persistent understanding and governed ways to act without becoming the store of file bytes.
_Avoid_: Operating-system filesystem, file browser, vector index

**Declared Intent**:
An explicit natural-language statement of the state or outcome a person wants Katsi to maintain in a workspace.
_Avoid_: Inferred preference, historical norm, workspace contract

**Executable Invariant**:
A machine-checkable condition that must remain true while Katsi reconciles a workspace with declared intent.
_Avoid_: Suggestion, inferred preference

**Change Set**:
A typed proposal containing its intent, affected resources, dependency set with expected content hashes, preconditions, operations, postconditions, and rollback behavior. It becomes stale when a relevant input, invariant, dependency, or intended output changes.
_Avoid_: Shell command, filesystem call, patch

**Agent Identity**:
The durable identity under which a Personal Agent receives capabilities, acquires work, publishes Claims, proposes Change Sets, and leaves an auditable history.
_Avoid_: Model name, chat session, process ID

**Work Lease**:
A time-bounded declaration by an Agent Identity over a scope of work. It is advisory during exploration and becomes exclusive over affected resources only while a Change Set is being validated and applied.
_Avoid_: Claim, file lock, capability grant

**Claim**:
A provenance-backed assertion contributed by an Agent Identity with its author, evidence, time, scope, confidence, and verification status. Claims may be proposed, corroborated, verified, contradicted, or superseded; model confidence alone never verifies one.
_Avoid_: Fact, memory, work claim

**Workspace Brief**:
A compact, provenance-backed account of an Active Project Workspace's goal, relevant verified Claims, decisions, relationships, recent changes, active work, and open questions for a Personal Agent beginning or resuming work.
_Avoid_: Context dump, chat summary, README

**Time to Verified Action**:
The elapsed time and context cost from a fresh agent session to a successfully verified Change Set.
_Avoid_: Response latency, task duration, token count

**Workspace Control Center**:
The owner-facing interface for intent, Agent Identities, capabilities, active work, Claims, Change Sets, verification, recovery, and YOLO status. Search and question answering are supporting utilities.
_Avoid_: Chat interface, file browser, admin panel

**Workspace Reconciliation**:
The continuous process of detecting divergence between a workspace and its declared intent, then planning, authorizing, applying, and verifying a Change Set that restores alignment.
_Avoid_: File watching, synchronization, incident repair

**Active Project Workspace**:
An explicitly bounded collection of files serving an ongoing personal project with a goal against which progress can be evaluated.
_Avoid_: Home directory, archive, monitored folder

**Project Reconciliation Platform**:
A system for continuously keeping an active personal project's evidence, decisions, and artifacts aligned with user-approved intent.
_Avoid_: Filesystem, folder organizer, retrieval engine

**Goal**:
The outcome that determines what progress means within an active project workspace.
_Avoid_: Invariant, preference, task

**Preference**:
A desired quality of a reconciled workspace that guides planning but may yield when it conflicts with a goal or invariant.
_Avoid_: Invariant, requirement

**External Change**:
A workspace change made outside Katsi that is treated as evidence, not automatically as new intent or as drift to reverse.
_Avoid_: User intent, violation

**Governed Path**:
The Katsi protocol through which a cooperating Personal Agent coordinates work and proposes a Change Set with validation, verification, recovery, and audit guarantees. Ordinary direct writes remain possible, are observed as External Changes, and do not receive those guarantees.
_Avoid_: Filesystem permission, write interception, sandbox

**Intent Amendment**:
A proposed change to declared intent, invariants, preferences, or authority that becomes effective only when authorized by the person.
_Avoid_: Contract amendment, learned preference, automatic policy update

**Capability Grant**:
Revocable authority explicitly given by the Workspace Owner to an Agent Identity for specified operations within one Active Project Workspace.
_Avoid_: Global permission, filesystem access

**Intent Snapshot**:
A versioned, inspectable interpretation of the authoritative natural-language prompt, including its goals, preferences, invariants, ambiguities, and required capabilities.
_Avoid_: Prompt, inferred intent, workspace contract

**Active Intent Snapshot**:
An Intent Snapshot that the person has reviewed and activated as the sole authority for workspace reconciliation. The first snapshot and every Intent Amendment require activation.
_Avoid_: Latest interpretation, model decision

**Connected Source**:
An explicitly authorized, read-only external source from which Katsi may collect evidence for one active project workspace.
_Avoid_: Workspace, global connector, discovered account

**Web Evidence**:
Information collected through agent-selected general web research and retained with its source and retrieval time; its presence does not make it authoritative or current.
_Avoid_: Truth, connected source, user intent

**Authority Tier**:
The degree to which a source is entitled to establish a particular fact: primary authorities and providers may establish facts within their remit, while secondary and community sources provide explanation or leads.
_Avoid_: Confidence score, popularity, search rank

**Readiness Assessment**:
The current evidence-backed account of a workspace's progress toward its goal, including unresolved gaps and uncertainty.
_Avoid_: Guarantee, itinerary, answer

**Original**:
An input artifact preserved in the form in which it entered a workspace and never modified or permanently destroyed by autonomous reconciliation.
_Avoid_: Source of truth, current version

**Derived Artifact**:
An artifact created from originals or other workspace evidence that Katsi may replace when reconciliation requires an updated result.
_Avoid_: Original, backup

**Recoverable Quarantine**:
A reversible holding state for an artifact removed from active use without permanently destroying it or its history.
_Avoid_: Trash, deletion, archive

**Applied Unverified**:
The outcome of a Change Set that was applied without an applicable deterministic check or explicit owner verification; it must never be represented as verified success.
_Avoid_: Success, verified, probably correct

**Action Journal**:
An append-only record of governed mutations, affected hashes, recoverable preimages, verification results, and rollback outcomes.
_Avoid_: Log file, chat history, backup

**Portable Project State**:
Owner-approved intent, invariants, verified decisions, and project metadata that may travel with an Active Project Workspace.
_Avoid_: Cache, agent activity, recovery data

**Private Operational State**:
Machine-local embeddings, caches, Agent Identities, Capability Grants, Work Leases, detailed activity, and recovery data that do not travel with the project by default.
_Avoid_: Portable project state, workspace files
