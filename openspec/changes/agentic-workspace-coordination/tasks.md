## 1. Core Contracts and Configuration

- [x] 1.1 Add strict typed identifiers, enums, and Pydantic models for workspaces, resources, resource versions, workspace events, Claims, Agent Identities, Capability Grants, Work Leases, Change Sets, operations, transitions, and action outcomes.
- [x] 1.2 Add configuration models for SQLite storage, portable metadata path, observer behavior, lease TTLs, operation limits, recovery retention, projection workers, and verifier definitions with no hardcoded runtime values.
- [x] 1.3 Add serialization and validation tests for every new public contract, including invalid state transitions and forbidden extra fields.
- [x] 1.4 Define typed core errors for conflicts, stale state, authorization denial, invalid transitions, unsupported operations, projection lag, and recovery-required outcomes.
- [x] 1.5 Export the new core contracts without adding MCP, CLI, or application imports to `katsi_core`.

## 2. Transactional Workspace Store

- [x] 2.1 Implement a configured SQLite connection factory with WAL mode, foreign keys, schema versioning, busy timeout, and safe process-local cleanup.
- [x] 2.2 Create idempotent migrations for workspace, resource, resource-version, event, enrichment, identity, credential, capability, Claim, evidence, work, lease, Change Set, transition, journal, and projection-outbox tables.
- [x] 2.3 Implement short transaction helpers that support expected state/resource versions and return typed conflicts without holding transactions across external work.
- [x] 2.4 Implement append-only per-workspace event sequencing and atomic current-state plus history writes.
- [x] 2.5 Implement projection-outbox insertion in the same transaction as authoritative state changes.
- [x] 2.6 Add repository adapters for reading current workspace state and paginating ordered event history.
- [x] 2.7 Add concurrent-writer tests using separate connections to prove uniqueness, conflict, timeout, and rollback behavior.
- [x] 2.8 Add schema-upgrade and newer-schema refusal tests using temporary databases.

## 3. Workspace and Resource Identity

- [x] 3.1 Implement workspace registration with stable random identity, canonical root resolution, and overlapping-root rejection.
- [x] 3.2 Implement workspace-root relocation without changing workspace identity and record the relocation event.
- [x] 3.3 Implement stable resource identities with mutable current paths and immutable content-addressed resource versions.
- [x] 3.4 Implement resource create, update, move, ambiguity, and delete state transitions with historical provenance.
- [x] 3.5 Add tests proving an explicit move preserves resource identity while duplicate-content ambiguity does not merge identities.
- [x] 3.6 Implement schema-versioned Portable Project State export containing only approved intent, invariants, decisions, and selected metadata.
- [x] 3.7 Implement Portable Project State import that restores no credentials, capabilities, leases, activity, or recovery data.
- [x] 3.8 Add round-trip and privacy-boundary tests for portable/private state.

## 4. Legacy Migration

- [x] 4.1 Implement a read-only importer for valid legacy `file_records.json` records with migration provenance and stable workspace assignment.
- [x] 4.2 Preserve existing content hashes and summaries as compatible enrichment versions when their configuration fingerprint can be established.
- [x] 4.3 Record records missing on disk as historical/deleted rather than current resources.
- [x] 4.4 Add idempotency tests proving repeated migration does not duplicate resources, versions, events, or enrichment.
- [x] 4.5 Retain and document a legacy backup and refuse destructive cleanup until reconciliation and projection validation pass.

## 5. Filesystem Reconciliation

- [x] 5.1 Define observer and full-scan protocols in core and add a lazily loaded cross-platform `watchdog` observer adapter.
- [x] 5.2 Implement stable-read hashing with configured debounce, retry, include/exclude, size, and reserved-path policies.
- [x] 5.3 Implement a full workspace scan that converges creates, modifications, moves, ambiguous moves, and deletions into authoritative state.
- [x] 5.4 Implement continuous create, modify, move, rename, and delete event handling as idempotent reconciliation hints.
- [x] 5.5 Trigger full reconciliation after startup, observer overflow, detected sequence gap, and explicit owner request.
- [x] 5.6 Correlate governed executor events with their Change Set and classify unexplained direct writes as External Changes.
- [x] 5.7 Add fixture tests for duplicate, reordered, coalesced, and missing observer events.
- [ ] 5.8 Add restart and full-scan tests proving deleted resources cannot remain in current search or relationships.

## 6. Content Enrichment Reuse

- [x] 6.1 Define a deterministic enrichment fingerprint covering content hash, extraction contract, model identity, prompt version, chunking, and semantic settings.
- [x] 6.2 Implement a content-enrichment cache independent of resource path and current resource identity.
- [x] 6.3 Route ingestion through cache lookup so copied content and A→B→A histories perform zero repeated local-model calls when compatible.
- [x] 6.4 Persist strict Extraction validation, one retry, and terminal error state before publishing semantic projections.
- [ ] 6.5 Add tests proving invalid extraction cannot publish current chunks, graph edges, or Claims.
- [x] 6.6 Add tests covering compatible reuse and intentional re-enrichment after a fingerprint change.

## 7. Rebuildable Graph and Vector Projections

- [x] 7.1 Implement an idempotent projection worker that consumes ordered outbox entries and records per-projection offsets.
- [x] 7.2 Change graph enrichment to replace a resource version's current entities, topics, and references instead of accumulating stale edges.
- [ ] 7.3 Change vector projection to replace current chunks and exclude deleted or errored resources.
- [ ] 7.4 Implement reference backfill and deterministic resolution without depending on ingest order.
- [ ] 7.5 Expose projection lag and last applied offsets in status and retrieval diagnostics.
- [ ] 7.6 Implement full Kùzu and LanceDB rebuilds from authoritative resources and cached enrichment.
- [ ] 7.7 Add failure-injection tests proving authoritative events survive graph/vector failure and rebuild invokes no unnecessary local-model calls.

## 8. Agent Identity and Capability Grants

- [x] 8.1 Implement owner registration and revocation of durable Agent Identities with descriptive client/model metadata separated from authority.
- [x] 8.2 Implement opaque credential issuance, salted credential hashing, constant-time verification, redaction, and rotation.
- [x] 8.3 Load the initial stdio agent credential from configured process environment rather than MCP tool arguments.
- [x] 8.4 Implement revocable Capability Grants scoped by identity, workspace, operation class, resource scope, risk limit, and expiry where configured.
- [x] 8.5 Implement a core authorization service that records the evaluated grant and rejects self-expansion or owner-only transitions.
- [x] 8.6 Add tests for forged identity labels, revoked credentials, cross-workspace access, expired grants, and scope/risk denial.

## 9. Claims and Durable Workspace State

- [x] 9.1 Implement immutable Claim publication with author, scope, confidence metadata, evidence dependencies, and proposed status.
- [x] 9.2 Implement append-only Claim evidence and proposed, corroborated, verified, contradicted, and superseded transitions.
- [x] 9.3 Restrict verification to deterministic evidence, typed authoritative evidence, or explicit owner action; reject model confidence as verification.
- [x] 9.4 Implement verified decisions, blockers, open questions, and open-work records with provenance and lifecycle history.
- [x] 9.5 Invalidate Claim verification and dependent state when a relevant resource version, invariant, or evidence relationship changes.
- [x] 9.6 Preserve historical attribution after identity revocation without allowing the revoked identity to add transitions.
- [x] 9.7 Add tests for competing Claims, supersession, contradiction, evidence invalidation, and transcript-free persistence.

## 10. Work Leases and Workspace Briefs

- [x] 10.1 Implement advisory Work Lease acquisition, compare-and-set renewal, release, expiry, task description, and resource scope.
- [x] 10.2 Expose overlapping active advisory work without blocking exploration or Change Set proposal.
- [ ] 10.3 Implement brief assembly from authoritative goal, intent, Claims, decisions, open work, leases, changes, and invalidation state.
- [ ] 10.4 Fuse graph/vector context into Workspace Briefs while preserving provenance, evidence status, projection offsets, and lag warnings.
- [ ] 10.5 Replace fixed per-file token estimates with serialized budget accounting and explicit omission/provisional markers.
- [ ] 10.6 Add tests for lease expiry, overlap visibility, strict brief budgets, stale context labeling, and operation while projections lag.

## 11. MCP and CLI Coordination Surfaces

- [ ] 11.1 Add MCP operations to open/inspect a workspace and obtain a task-scoped Workspace Brief using the authenticated Agent Identity.
- [ ] 11.2 Add capability-checked MCP operations to publish/list Claims and inspect decisions, blockers, and open work.
- [ ] 11.3 Add MCP operations to acquire, renew, release, and inspect Work Leases.
- [ ] 11.4 Preserve existing `get_context`, `search_files`, `related`, summary, and status tools as compatible retrieval primitives.
- [ ] 11.5 Add CLI owner commands for workspace registration, portable-state import/export, identity issuance/revocation, and capability inspection without printing credentials after initial issuance.
- [ ] 11.6 Add MCP/CLI contract tests with fake stores, multiple authenticated clients, denial cases, and redacted errors.

## 12. Change Set Models and Lifecycle

- [ ] 12.1 Implement strict immutable Change Set, dependency, precondition, operation, postcondition, rollback, idempotency, and successor-version models.
- [ ] 12.2 Implement append-only transition history and reject every lifecycle transition not allowed by the design state machine.
- [ ] 12.3 Implement proposal submission that freezes content and creates a linked successor for every revision.
- [ ] 12.4 Persist exact resource-version dependencies, absence assertions, invariant versions, intended outputs, operation/byte limits, and risk class.
- [ ] 12.5 Add query APIs for current status, full transition history, validation evidence, authorization evidence, and terminal action receipts.
- [ ] 12.6 Add exhaustive state-machine, immutability, successor, and idempotency tests.

## 13. Validation and Authorization

- [ ] 13.1 Implement dependency-closure validation against exact resource versions, target hashes, absence assertions, invariants, and intended outputs.
- [ ] 13.2 Revalidate relevant state before authorization and immediately before each target replacement.
- [ ] 13.3 Mark proposals stale with exact triggering events while allowing unrelated workspace events to proceed.
- [ ] 13.4 Implement owner approval and denial transitions with immutable decision evidence.
- [ ] 13.5 Evaluate Agent Identity, Capability Grant, active intent, action class, scope, limits, and policy mode without permitting authority-plane operations.
- [ ] 13.6 Add the MCP/owner API for proposing, validating, reviewing, approving, rejecting, and superseding Change Sets without applying files.
- [ ] 13.7 Add multi-client race tests for relevant conflict, unrelated parallel work, revoked authority, changed intent, and expired approval.

## 14. Closed Filesystem Operation Catalog

- [ ] 14.1 Implement strict discriminated operation models for create, exact-hash replace, deterministic patch, copy, in-workspace move, directory creation, quarantine, restore, and derived-artifact replacement.
- [ ] 14.2 Implement path canonicalization using non-following component checks and reject traversal, symbolic-link escape, special files, and cross-workspace targets.
- [ ] 14.3 Implement operation-specific preflight checks for existence, expected hash, output hash, disk space, byte budget, operation count, and rollback feasibility.
- [ ] 14.4 Implement deterministic in-memory patch application that stages complete resulting bytes rather than mutating a live target.
- [ ] 14.5 Reject arbitrary commands, permanent deletion, permission/ownership changes, mounts, downloaded execution, external side effects, and Git history rewriting.
- [ ] 14.6 Add unit tests for every operation, path attack, forbidden operation, size/risk boundary, and platform-supported replacement behavior.

## 15. Governed Executor and Action Journal

- [ ] 15.1 Implement short exclusive write-set leases with transactional overlap prevention and Change Set correlation.
- [ ] 15.2 Implement a private content-addressed recovery-blob store with deduplication, integrity verification, and configured retention metadata.
- [ ] 15.3 Implement durable Action Journal planning entries before any target mutation, including hashes, preimages, operations, and recovery plan.
- [ ] 15.4 Implement adjacent same-filesystem staging with configured reserved names, fsync where supported, and per-file atomic replacement.
- [ ] 15.5 Record each operation step durably and make repeated application requests resume or return the existing idempotent result.
- [ ] 15.6 Implement quarantine and restore without permanent deletion and preserve original/action history.
- [ ] 15.7 Release exclusive leases only after a terminal or recovery-required outcome.
- [ ] 15.8 Add fault injection before and after every journal, stage, replace, and step-record boundary.

## 16. Verification, Rollback, and Restart Recovery

- [ ] 16.1 Implement owner-configured verifier definitions with fixed executable/argument prefix, allowed variable arguments, cwd scope, environment allowlist, timeout, output limit, applicability, and required policy.
- [ ] 16.2 Execute verifiers with `shell=False`, bounded output, secret redaction, and no database transaction held during execution.
- [ ] 16.3 Recheck input/resource versions before committing verifier results and link bounded verification evidence to the Change Set.
- [ ] 16.4 Produce verified only after all required checks/invariants pass; produce applied-unverified when no verifier applies and the owner has not verified.
- [ ] 16.5 Implement reverse-order compensation from preimages and record every rollback step and resulting hash.
- [ ] 16.6 Implement startup recovery analysis for applying and rolling-back journals before admitting overlapping writes.
- [ ] 16.7 Produce owner-visible recovery-required evidence when resume or rollback cannot be proven safe.
- [ ] 16.8 Add tests for verifier success/failure/timeout, owner verification, interrupted rollback, corrupted preimage, and restart recovery.

## 17. Workspace Control Center

- [ ] 17.1 Select and document the control center's local transport using the existing application boundary without changing core authorization semantics.
- [ ] 17.2 Add owner APIs for intent activation, identity/capability administration, active work, Claims, Change Set review, verification, and recovery.
- [ ] 17.3 Replace the app's hardcoded Library/Ask demonstrations with workspace state and provenance-backed API data while retaining search as a utility.
- [ ] 17.4 Add control center views for active intent, agents, capabilities, leases, contradictions, External Changes, proposed changes, action history, and recovery-required states.
- [ ] 17.5 Add explicit owner confirmations for activation, approval, verification, recovery actions, and YOLO scope changes.
- [ ] 17.6 Add frontend/backend tests and complete EN/ES strings for every new owner-facing state and error.

## 18. YOLO Authorization Mode

- [ ] 18.1 Implement owner activation and revocation scoped to Agent Identity, workspace, operation classes, limits, and policy version.
- [ ] 18.2 Restrict initial YOLO policy to allowed derived artifacts and reversible organization; require owner approval for owner-authored original modification.
- [ ] 18.3 Route YOLO through the identical validation, lease, operation, journal, verification, rollback, and recovery services as governed approval.
- [ ] 18.4 Automatically suspend YOLO scope after authorization mismatch, invariant failure, verification failure, or recovery-required outcome.
- [ ] 18.5 Add policy-simulation output showing which proposed Change Sets would be auto-authorized before activation.
- [ ] 18.6 Add tests proving YOLO cannot grant authority, expand scope, bypass safeguards, modify prohibited originals, or permanently delete data.

## 19. Dogfood, Metrics, and Release Gates

- [ ] 19.1 Instrument Time to Verified Action, brief context cost, repeated-enrichment avoidance, reconciliation latency, projection lag, stale-plan decisions, and recovery outcomes.
- [ ] 19.2 Build the Agent A → Agent B continuity fixture using separate MCP client processes and durable Claims/work state.
- [ ] 19.3 Add Agent C concurrent relevant-change coverage proving the stale proposal is blocked and exact invalidation evidence is returned.
- [ ] 19.4 Add unrelated concurrent-change coverage proving independent Change Sets remain valid.
- [ ] 19.5 Run governed executor fault-injection and restart-recovery suites across every operation class on supported CI platforms.
- [ ] 19.6 Update README, configuration example, MCP tool documentation, privacy guarantees, migration instructions, and recovery operator guide.
- [x] 19.7 Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` and resolve every failure before enabling each release gate.
