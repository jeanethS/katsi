# Katsi Agentic Filesystem Roadmap

**Status:** Planning  
**Date:** 2026-08-07

## Objective

Move Katsi from relational file retrieval to persistent local workspace memory,
multi-agent coordination, governed file changes, and local multimedia
understanding without losing the current privacy and summarize-once guarantees.

The roadmap is ordered by evidence and trust, not calendar dates. Each milestone
must satisfy its exit gate before downstream autonomy is enabled.

## Dependency map

```text
                    ┌──────────────────────────┐
                    │ A. Transactional spine  │
                    │ stable workspace state  │
                    └────────────┬─────────────┘
                                 │
               ┌─────────────────┴─────────────────┐
               ▼                                   ▼
┌──────────────────────────┐          ┌──────────────────────────┐
│ B. Filesystem reconcile  │          │ C. Living Model         │
│ current resource truth   │          │ claims, briefs, intent  │
└────────────┬─────────────┘          └────────────┬─────────────┘
             │                                     │
             └─────────────────┬───────────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │ D. Agent coordination   │
                  │ identities, grants, work│
                  └────────────┬─────────────┘
                               │
                               ▼
                  ┌──────────────────────────┐
                  │ E. Change validation    │
                  │ stale-plan prevention   │
                  └────────────┬─────────────┘
                               │
                               ▼
                  ┌──────────────────────────┐
                  │ F. Governed execution   │
                  │ verify, recover, audit  │
                  └────────────┬─────────────┘
                               │
                               ▼
                  ┌──────────────────────────┐
                  │ G. Constrained YOLO     │
                  └──────────────────────────┘

Multimedia parallel track:

B ──▶ M1 representations ──▶ M2 image/scans ──▶ M3 audio ──▶ M4 video
            │                                              │
            └────────────────▶ M5 multimodal retrieval ◀───┘

F + M5 ──▶ M6 governed export of derived media
```

## Release roadmap

### Release 0 — Trustworthy foundation

**Features:** transactional spine and filesystem reconciliation.

Deliver:

- stable workspace and resource identities;
- SQLite authority with ordered workspace events and projection outbox;
- create/modify/move/delete observation plus startup reconciliation;
- content enrichment cache keyed by hash and configuration fingerprint;
- replacement of stale graph/vector semantics;
- migration from JSON FileRecords without reprocessing unchanged bytes.

**Exit gate:** after arbitrary file creates, edits, moves, deletes, restarts, and
projection failures, Katsi converges to current filesystem state; unchanged content
causes zero local-model calls.

### Release 1 — Durable agent memory

**Features:** Living Model and read-mostly agent coordination.

Deliver:

- typed Workspace State;
- Claims and evidence with explicit verification states;
- owner-registered Agent Identities and scoped Capability Grants;
- advisory Work Leases;
- compact Workspace Briefs;
- portable/private state separation;
- MCP operations for briefs, Claims, decisions, and work coordination.

**Exit gate:** Agent B can resume work produced by Agent A from another MCP client
without replaying chat or rescanning the project, and all durable contributions are
attributable and provenance-backed.

### Release 2 — Validated Change Sets

**Features:** proposal and stale-plan prevention without Katsi-owned file execution.

Deliver:

- immutable, versioned Change Sets;
- dependency sets, expected hashes, invariants, and typed operation plans;
- capability and owner-approval evaluation;
- relevant-change invalidation while unrelated parallel work continues;
- owner review surfaces and action receipts for changes applied by existing tools.

**Exit gate:** the dogfood scenario reliably blocks an agent proposal after a
relevant concurrent change and permits it after unrelated changes.

### Release 3 — Governed execution

**Features:** allowlisted operations, deterministic verification, and recovery.

Deliver:

- short exclusive write-set leases;
- closed typed filesystem operation algebra;
- content-addressed preimages and append-only Action Journal;
- staged per-file replacement and idempotent execution;
- owner-configured verifier catalog;
- automatic compensation and startup recovery;
- explicit `Applied Unverified` and `Recovery Required` outcomes.

**Exit gate:** fault injection at every execution step either reaches a verified
result, restores preimages, or produces an exact owner-visible recovery state. No
failure is reported as verified success.

### Release 4 — Local multimedia understanding

**Features:** images, scanned documents, audio, video, and modality-aware evidence.

Deliver in vertical slices:

1. modality-neutral Derived Representations and Evidence Locators;
2. media detection, metadata, caching, and pipeline registry;
3. image OCR/caption/visual representations and scanned-document OCR;
4. timestamped audio transcription;
5. budgeted video transcript, scene, and keyframe processing;
6. separate visual indexes and calibrated multimodal retrieval;
7. compact media evidence in MCP context and Claims.

**Exit gate:** a new agent can retrieve and cite the exact image region, document
page, audio interval, or video keyframe supporting a Claim without uploading the
media or injecting complete media/transcripts into context.

### Release 5 — Governed derived-media actions

**Features:** safe generation and export of non-original media artifacts.

Deliver:

- typed thumbnail, transcript, keyframe, proxy, and representation-export actions;
- source-version links and exact-hash preconditions;
- staged output validation, journaling, verification, and rollback;
- no destructive original editing or remote publication.

**Exit gate:** every exported media artifact is reproducible from its source and
pipeline fingerprint, and failure never alters the original.

### Release 6 — Constrained YOLO Mode

**Features:** approval-free execution using the proven governed executor.

Deliver:

- identity/workspace/action-class YOLO scopes;
- policy simulation and owner-visible activation;
- initial restriction to derived artifacts and reversible organization;
- automatic suspension after invariant, verification, or recovery failure.

**Exit gate:** sustained dogfood operation produces no unauthorized scope expansion,
no permanent data loss, and complete action/verification evidence.

## Feature roadmap: Filesystem reconciliation

1. Establish transactional workspaces, stable resources, resource versions, and
   workspace events.
2. Import existing FileRecords and preserve compatible summaries.
3. Add full-scan convergence before enabling continuous observation.
4. Add create, modify, move, rename, and delete observation with debouncing.
5. Separate content identity from resource/path identity.
6. Replace current graph/vector semantics rather than accumulating old edges.
7. Add dependency invalidation and projection lag visibility.
8. Prove restart, missed-event, duplicate-content, and projection-rebuild behavior.

## Feature roadmap: Workspace Living Model

1. Define typed state, events, provenance, and portable/private boundaries.
2. Add immutable Claims, evidence, and verification transitions.
3. Add goals, activated intent, verified decisions, blockers, and open work.
4. Build budgeted Workspace Brief assembly from authority plus projections.
5. Invalidate Claims and brief material from resource dependencies.
6. Add portable state export/import without machine authority.
7. Measure brief relevance, provenance completeness, and context cost.

## Feature roadmap: Agent coordination

1. Register and authenticate durable Agent Identities locally.
2. Add revocable workspace-scoped Capability Grants.
3. Attribute every durable contribution and preserve revoked history.
4. Add advisory Work Lease acquisition, renewal, expiry, and release.
5. Surface overlapping work in Workspace Briefs and the control center.
6. Add MCP operations and multi-client concurrency tests.
7. Dogfood Agent A → Agent B continuity before adding mutations.

## Feature roadmap: Governed Change Sets

1. Define immutable Change Set models, transitions, dependencies, and idempotency.
2. Validate exact hashes, absence assertions, invariants, limits, and capabilities.
3. Detect relevant staleness while permitting unrelated work.
4. Add owner authorization without Katsi-owned writes.
5. Add exclusive write-set leases and the typed operation catalog.
6. Add staging, preimages, Action Journal, and per-step execution receipts.
7. Add deterministic verifier catalog and honest terminal outcomes.
8. Add rollback, crash recovery, and fault-injection tests.
9. Enable YOLO only at the authorization step after reliability gates pass.

## Feature roadmap: Multimedia understanding

1. Add Derived Representation, status, coverage, and Evidence Locator contracts.
2. Add media detection and an owner-configured bounded pipeline registry.
3. Add content-addressed representation and private derived-blob caching.
4. Ship image metadata/OCR first, then captions and optional visual embeddings.
5. Add scanned-document fallback with page/region provenance.
6. Add audio metadata, timestamped transcription, and anonymous speaker segments.
7. Add budgeted video audio, scenes, keyframes, and coverage reporting.
8. Add per-space vector indexes and evidence-based score fusion.
9. Add media-aware context, Claim citations, sensitive-metadata capabilities, and
   governed derived-artifact exports.

## Cross-cutting release gates

Every milestone must retain:

- strict type hints and Ruff compliance;
- unit tests with local services faked or fixtured;
- no CI dependency on Ollama, Kùzu daemons, remote APIs, or media services;
- no hardcoded model identities, paths, thresholds, limits, or timeouts;
- strict extraction/representation validation with one retry for model JSON;
- no reprocessing of compatible unchanged content hashes;
- provenance for every durable Claim and derived representation;
- explicit states for partial, stale, unverified, failed, and recovery-required work;
- no arbitrary agent-generated commands or silent authority expansion.

## Measurement plan

Primary product metric:

- **Time to Verified Action:** elapsed time and context cost from a fresh agent
  session to a verified Change Set.

Supporting measures:

- repeat exploration tokens avoided;
- compatible content reuse rate;
- reconciliation convergence time;
- stale proposals correctly blocked;
- unrelated proposals allowed;
- Workspace Brief provenance completeness;
- media coverage and representation reuse;
- verifier pass/fail accuracy;
- rollback and startup-recovery success;
- disk and compute cost per resource modality.

## Related OpenSpec changes

- [`agentic-workspace-coordination`](../openspec/changes/agentic-workspace-coordination/)
- [`multimedia-understanding`](../openspec/changes/multimedia-understanding/)
