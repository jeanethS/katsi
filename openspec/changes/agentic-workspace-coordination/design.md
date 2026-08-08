## Context

See [proposal.md](./proposal.md) for motivation and scope. The behavioral contracts are split across the [workspace Living Model](./specs/workspace-living-model/spec.md), [agent coordination](./specs/agent-coordination/spec.md), [filesystem reconciliation](./specs/filesystem-reconciliation/spec.md), and [governed Change Sets](./specs/governed-change-sets/spec.md).

The current core has three independent persistence mechanisms:

- `FileRecordStore` rewrites one cached JSON object keyed by path-derived file id;
- Kùzu stores current file, entity, topic, and relationship projections;
- LanceDB stores current chunk and vector projections.

This is sufficient for single-process retrieval but not for concurrent MCP clients, durable Claims and leases, ordered state transitions, stale-plan validation, or crash recovery. Current path-derived identity also prevents reliable move history, and current semantic upserts do not establish an authoritative transaction boundary across records and projections.

The repository constraints remain binding:

- Python 3.12 and the existing `uv` workspace;
- `katsi_core` has no MCP, CLI, or application imports;
- model names, paths, timeouts, limits, and thresholds are configured;
- frequent extraction and retrieval remain local;
- enrichment is reused exactly once per compatible content hash;
- external services are faked in tests and never required by CI.

## Goals / Non-Goals

**Goals:**

- Establish one transactional authority for private Workspace State and coordination.
- Preserve ordinary files as the authority for content bytes.
- Let several local MCP clients share state without holding long-lived database transactions.
- Separate logical resource identity from mutable paths and content versions.
- Make graph and vector stores rebuildable projections rather than coordination authorities.
- Provide a recoverable Change Set executor with closed operations and honest outcomes.
- Migrate existing indexed workspaces without forcing unchanged content through the local model again.

**Non-Goals:**

- Kernel, FUSE, or mounted-filesystem integration.
- Preventing non-cooperating processes from writing ordinary files.
- Distributed consensus, team tenancy, cross-device synchronization, or cloud authority.
- Storing conversation transcripts or hidden reasoning.
- Making Kùzu or LanceDB transactional participants in Change Set application.
- General shell execution, package management, Git history rewriting, or external network side effects.
- Multi-file ACID guarantees that ordinary filesystems do not provide.

## Decisions

### 1. Use SQLite as the private transactional authority

Add a workspace-state adapter in `katsi_core` backed by the Python standard library's SQLite driver. Use WAL mode, foreign keys, a configured busy timeout, and short write transactions. The database stores authoritative private coordination state and metadata; it does not store source file bytes as their current authority.

The initial logical tables are:

- `workspaces` and `workspace_roots`;
- `workspace_events` with a monotonic per-workspace sequence;
- `resources` and `resource_versions`;
- `content_enrichments` keyed by content hash and enrichment fingerprint;
- `intent_snapshots` and `invariants`;
- `agent_identities`, `agent_credentials`, and `capability_grants`;
- `claims`, `claim_evidence`, and `claim_transitions`;
- `open_work` and `work_leases`;
- `change_sets`, `change_set_dependencies`, `change_set_operations`, and `change_set_transitions`;
- `action_journal` and recovery-blob references;
- `projection_outbox` and `projection_offsets`.

Current-state tables and their append-only history/event rows are updated in the same SQLite transaction. This is not strict event sourcing: current tables remain authoritative and need not be rebuilt from every historical event. Events provide ordering, invalidation inputs, audit history, and projection work.

**Alternatives considered:**

- **Continue JSON files:** simple but lacks concurrent writes, referential constraints, indexed queries, and atomic multi-record transitions.
- **Make Kùzu authoritative:** useful for traversal but awkward for leases, uniqueness constraints, ordered transitions, and transactional authorization checks.
- **Use an append-only JSONL log alone:** portable but requires custom locking, indexes, compaction, and recovery semantics already provided by SQLite.
- **Use PostgreSQL:** strong semantics but violates the embedded, local, zero-service deployment boundary.

### 2. Split portable project state from private operational state

Private state lives under the configured Katsi data root in a directory keyed by stable workspace id. It contains the SQLite database, content-addressed recovery blobs, projection databases, and retrieval caches.

Portable owner-approved state uses a schema-versioned document at a configured workspace-relative metadata path. Its logical content is limited to:

- workspace id and display metadata;
- authoritative natural-language intent and activated snapshot version;
- executable invariant definitions;
- owner-verified decisions selected for portability.

The default serialization is canonical JSON so core requires no additional parser dependency. The location is configuration-driven rather than embedded in logic. Import never restores Agent Identities, credentials, Capability Grants, leases, detailed activity, or recovery material.

Portable writes use an owner-authorized state transition and are themselves observed file changes. The reconciler recognizes its reserved metadata path to avoid treating the resulting event as untrusted intent.

**Alternatives considered:**

- **All state inside the project:** portable but leaks authority and private activity into version control and sync tools.
- **All state outside the project:** private but makes verified project intent and decisions difficult to move or back up with the project.

### 3. Give workspaces and resources stable logical identities

Workspace registration assigns a random stable id independent of root path. Active canonical roots cannot overlap because overlap would make event ownership and Capability Grant scope ambiguous.

Each tracked file receives a stable `resource_id`. Path is a mutable property; content is represented by immutable `resource_versions` keyed by content hash. A resource version links extraction and enrichment provenance without making content hash the resource identity.

Move identity is preserved when the observer provides an unambiguous move event. During a full scan, Katsi may use platform file identity and content hash as evidence, but it does not merge identities when duplicate content creates multiple candidates. Ambiguous cases become explicit deletion and creation events or an ambiguity requiring owner resolution.

**Alternatives considered:**

- **Path hash as identity:** deterministic but loses history on moves and leaves stale path records.
- **Content hash as identity:** deduplicates bytes but collapses distinct files and cannot represent one resource changing content.

### 4. Keep database transactions short and use optimistic concurrency

Every state-changing core command follows this pattern:

1. Read the required current state without holding a write transaction.
2. Perform filesystem reads, model calls, projection queries, staging, or verification outside the database transaction.
3. Begin a short write transaction.
4. Recheck the expected workspace/resource versions and authorization state.
5. Commit current-state rows, history rows, and projection-outbox entries together.

Commands accept an expected state version or specific expected resource versions where concurrency matters. A mismatch produces a typed stale/conflict result rather than silently overwriting state.

SQLite transactions are never held across local-model calls, filesystem scans, MCP client interaction, verifier processes, or Change Set execution. This prevents a slow agent or test suite from blocking every client.

### 5. Use a transactional outbox for Kùzu and LanceDB projections

Kùzu remains the semantic relationship projection and LanceDB remains the chunk/vector retrieval projection. An authoritative SQLite state transition inserts projection work into `projection_outbox` in the same transaction. Projection consumers apply idempotent work and advance a per-projection offset.

Retrieval responses expose projection freshness. Coordination and authorization reads use SQLite and do not depend on projection availability. When projection application fails, the committed workspace event remains authoritative and can be retried. A projection can be discarded and rebuilt from current resources and cached content enrichment without invoking the local model for compatible content hashes.

Each successful file enrichment replaces the resource's current Kùzu relationships and LanceDB chunks. Historical semantics remain in SQLite provenance/history rather than polluting current graph edges.

**Alternative considered:** a best-effort dual write from the ingest pipeline. Rejected because a crash between stores creates silent divergence with no durable retry point.

### 6. Add an explicit filesystem reconciler

Introduce a reconciler service in core with an observer adapter and a full-scan path. The default cross-platform observer uses `watchdog` because it reports explicit move events on supported platforms; observer choice remains configurable behind the adapter.

Events are hints, not the only source of truth. The reconciler:

1. canonicalizes and scopes the path;
2. ignores configured internal staging paths without following symbolic links;
3. reads stable metadata and content hash;
4. records a workspace event and new resource version;
5. reuses or produces content enrichment;
6. writes projection outbox entries;
7. invalidates dependent Claims, briefs, leases, and proposed Change Sets.

Startup, observer overflow, and detected sequence gaps trigger a full scan. A scan compares current paths and hashes with resource state, records missing/deleted resources, and converges projections. Watch and scan operations use per-resource debouncing configured by time and stability checks; values are not hardcoded.

### 7. Fingerprint the enrichment contract

`content_enrichments` is keyed by:

- content hash;
- extraction implementation/schema version;
- configured local model identity;
- prompt/Extraction contract version;
- relevant chunking or semantic settings version.

Compatible content at another path or returning after an intervening change reuses the stored enrichment. Invalid extraction is retried once and then recorded as an error version; it never creates current semantic projections.

The content hash remains a byte hash. Configuration fingerprints decide compatibility without changing content identity.

### 8. Put coordination commands in core and MCP adaptation at the edge

Add core services for workspace queries and commands. MCP, CLI, and the application call those services but do not import one another. Representative core operations include:

- open/register workspace and build Workspace Brief;
- register/revoke Agent Identity and manage Capability Grants;
- publish/transition Claims;
- acquire/renew/release Work Leases;
- propose/validate/authorize/apply/verify/recover Change Sets;
- read event and action history.

Existing `get_context`, `search_files`, `related`, and summary operations remain available and feed Workspace Brief assembly.

For the initial stdio MCP transport, the server process receives an opaque agent credential through configured process environment, not a tool argument. The private database stores a salted password hash. Each tool call is attributed to the authenticated identity established when services initialize. Credentials are redacted from errors and logs. The owner-facing application issues and revokes credentials; a future shared transport can replace this authenticator without changing core authorization semantics.

### 9. Model Claims as assertions with explicit transitions

Agents append Claims and evidence; they do not update a shared fact row. Claim text and scope become immutable after publication. New evidence appends to the Claim, and status changes append transition rows. Superseding a Claim links a successor rather than rewriting history.

Verification authorities are typed:

- deterministic verifier result;
- direct authoritative evidence whose applicability is encoded;
- explicit Workspace Owner verification.

Agent corroboration can move a Claim to corroborated but never verified by itself. Resource dependencies recorded on Claim evidence allow the reconciler to invalidate verification when evidence changes.

### 10. Treat Work Leases as coordination, not long-lived locks

Exploration leases have a configured TTL and are advisory. Overlapping leases are visible in Workspace Briefs but do not prevent reads or proposals. Renewal and release are compare-and-set operations owned by the same identity.

At Change Set application, Katsi creates short exclusive resource leases over the canonical write set in one SQLite transaction. A uniqueness constraint prevents overlapping active exclusive leases. Expired exploration leases never imply permission; exclusive application leases are recovered from the associated Change Set state rather than simply expiring during an active commit.

### 11. Make Change Sets immutable recoverable workflows

Submission validates the schema and freezes the proposal. A revised proposal creates a successor id. Change Set transitions are commands checked against an explicit state machine:

```text
PROPOSED
   │ validate
   ├──────────────▶ STALE / REJECTED
   ▼
VALIDATED
   │ owner or policy authorization
   ▼
AUTHORIZED
   │ acquire exclusive write-set leases
   ▼
APPLYING
   ├──────────────▶ ROLLING_BACK ──▶ ROLLED_BACK
   │
   ▼
APPLIED
   │ verification
   ├──────────────▶ APPLIED_UNVERIFIED
   ├──────────────▶ ROLLING_BACK
   ▼
VERIFIED
```

`RECOVERY_REQUIRED` is available from any nonterminal execution state when automatic compensation cannot be proven safe. State history is append-only.

The dependency set contains exact resource-version ids or absence assertions plus invariant versions. Validation computes relevant dependency closure before the short commit that records `VALIDATED`. Authorization rechecks identity, Capability Grants, active intent, policy mode, and proposal freshness. Application rechecks target hashes immediately before each replacement.

YOLO Mode enters at the authorization decision only. It cannot alter validation, operation catalog, leases, journaling, verification, or recovery.

### 12. Use a closed operation algebra

Represent operations as a discriminated union validated by strict Pydantic models. The initial variants are:

- create file with fail-if-present semantics;
- replace exact-hash file;
- apply deterministic patch to exact-hash file;
- copy file;
- move file within the workspace;
- create directory;
- quarantine file;
- restore quarantined file;
- replace derived artifact.

Each operation carries canonical workspace-relative paths, expected state, resulting content hash when applicable, byte-count contribution, and rollback metadata. Change Sets also have configured limits for operation count, total affected bytes, and risk class.

Path resolution uses `lstat`/non-following checks for every existing component and verifies that resolved parents remain under the canonical workspace root. Symbolic-link traversal, special devices, sockets, cross-workspace targets, permission changes, permanent deletion, and arbitrary commands are rejected before authorization.

Patch application occurs in memory against the exact expected bytes and produces complete staged output. The executor does not apply an unbounded patch command directly to a live target.

### 13. Implement file application as a journaled saga

Ordinary filesystems do not provide multi-file transactions. Katsi therefore promises a recoverable Change Set, not atomic multi-file commit.

For an authorized Change Set, the executor:

1. acquires exclusive resource leases;
2. validates current hashes and available space;
3. materializes every resulting file and verifies its expected hash;
4. stores content-addressed preimages in the private recovery blob store;
5. commits an Action Journal entry containing the plan and durable blob references;
6. stages each output adjacent to its target using a configured reserved name so per-file replacement remains on the same filesystem;
7. fsyncs staged files and required parent directories where supported;
8. rechecks the live target hash;
9. uses atomic per-file replacement where supported and records each completed step;
10. runs postconditions and approved verifiers;
11. records the terminal outcome and releases leases.

The reconciler ignores reserved staging names but does not ignore final target events. Executor-produced events carry the Change Set correlation id so they update resource versions without being mistaken for unexplained External Changes.

Preimages are retained according to configured recovery policy. Permanent removal of recovery blobs is an owner maintenance action, not an agent Change Set operation.

On restart, a recovery coordinator inspects journal entries in applying or rolling-back states before admitting overlapping writes. It compares actual hashes with planned before/after hashes and either resumes, compensates, or records `RECOVERY_REQUIRED` with exact evidence.

### 14. Use owner-configured deterministic verifier definitions

Portable project state may name verifier definitions, but only owner activation makes them executable. A verifier definition contains:

- stable id and version;
- executable and fixed argument prefix;
- permitted agent-selectable arguments, if any;
- canonical working directory scope;
- sanitized environment allowlist;
- timeout and output limits;
- applicable resource patterns and required/optional policy;
- success exit codes and optional structured result parser.

Verification uses `shell=False`. Agent text never becomes shell source. Verifiers run outside SQLite transactions and their input versions are rechecked before the result is committed. Verifier output is stored as bounded evidence, with secrets redacted.

If every required verifier and invariant passes, the Change Set becomes verified. If no verifier applies and the owner does not explicitly verify, it becomes applied-unverified. Required-verifier failure begins compensation; if compensation is unsafe, the outcome becomes recovery-required.

### 15. Assemble Workspace Briefs from authority plus projections

Workspace Brief assembly first reads authoritative goals, Claims, decisions, leases, events, and invalidation state from SQLite. It then uses Kùzu and LanceDB to select semantically relevant files and chunks. The budgeter accounts for serialized content rather than reserving fixed estimates per file.

Every brief includes:

- workspace state version and last reconciled event;
- graph/vector projection offsets or a lag warning;
- provenance and status for durable Claims;
- overlapping active work;
- relevant External Changes and stale context;
- the retrieval evidence already exposed by current FileHits.

A lagging projection may reduce semantic recall but cannot cause stale Claims or Change Sets to be represented as current.

## Risks / Trade-offs

- **[SQLite writer contention across many MCP processes]** → Keep write transactions short, configure busy timeout, use WAL, batch observer events, and expose contention metrics. The single-machine initial boundary avoids distributed writers.
- **[Filesystem events are duplicated, reordered, or lost]** → Treat events as hints, make reconciliation idempotent, persist observed versions, and run full scans after startup or sequence gaps.
- **[Direct writers race the governed executor]** → Recheck exact hashes immediately before replacement and verify after application. Document that Katsi cannot sandbox non-cooperating processes.
- **[Multi-file rollback is interrupted]** → Persist preimages and each operation step before proceeding; perform startup recovery before admitting overlapping writes.
- **[Recovery blobs consume substantial disk]** → Content-address and deduplicate blobs, expose usage, and apply owner-configured retention only after terminal outcomes.
- **[Portable metadata leaks project decisions]** → Export only owner-selected state, make portability opt-in per field, and never include credentials or operational history.
- **[Graph/vector projections lag authority]** → Surface offsets and lag, prevent authorization from depending on projections, and support idempotent rebuilds.
- **[Owner-configured verifiers can themselves mutate files]** → Treat verifier definitions as owner-granted code execution, constrain cwd/environment/time, record before/after resource versions, and keep agent-generated commands prohibited.
- **[Move detection merges the wrong duplicate]** → Preserve identity only for explicit or unambiguous moves and surface ambiguity rather than guessing.
- **[Broad initial scope delays value]** → Deliver in vertical increments: authoritative workspace state and reconciliation, durable Claims/briefs/leases, Change Set validation, then governed execution. YOLO remains disabled until the same executor is proven.

## Migration Plan

1. Add the configured SQLite workspace store and schema migrations without changing existing retrieval behavior.
2. Register existing indexed roots as workspaces and import each valid `FileRecord` into a logical resource and resource version. Preserve existing content hashes and summaries as enrichment versions with migration provenance.
3. Populate projection offsets representing the imported Kùzu and LanceDB state, then run reconciliation to detect missing, moved, or changed files.
4. Rebuild graph/vector projections when imported projection state cannot be proven current. Reuse compatible content enrichment so unchanged bytes do not call the local model.
5. Route existing ingest mutations through the authoritative workspace transaction and projection outbox while preserving current CLI and MCP retrieval tools.
6. Add read-mostly coordination operations and Workspace Briefs. Keep governed file execution disabled.
7. Add Change Set proposal and validation behind configuration, followed by explicit owner-authorized execution.
8. Enable recovery and verifier surfaces before allowing any autonomous authorization mode.
9. Remove the legacy JSON FileRecord write path only after migration, reconciliation, and projection rebuild tests pass. Retain a backup for rollback.

Rollback before governed execution re-enables the legacy read path using the retained JSON backup and existing projections. After governed execution begins, rollback of the software version MUST preserve the SQLite and recovery stores; an older binary must refuse to open a newer schema rather than discard state.

## Open Questions

- Exact default TTLs, operation-count limits, byte budgets, event debounce windows, and recovery retention periods remain configuration choices to tune through dogfood measurements.
- The control center's local transport can be HTTP, an application command channel, or direct core integration; owner authorization semantics do not depend on that choice.
- OS keychain integration may replace or wrap environment-delivered stdio credentials after the initial single-machine protocol is validated.
