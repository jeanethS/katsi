## Purpose

Define how Katsi continuously reconciles ordinary filesystem state into a trustworthy Living Model while preserving content-hash reuse and removing stale projections.

## ADDED Requirements

### Requirement: The filesystem remains authoritative for file bytes
The system SHALL treat current bytes at the canonical workspace path as authoritative file content. Katsi metadata, summaries, Claims, graph edges, and vector entries MUST NOT override filesystem bytes.

#### Scenario: Metadata disagrees with current bytes
- **WHEN** stored metadata refers to an older content hash than the file currently contains
- **THEN** the system marks the resource changed and reconciles from the current bytes

### Requirement: Reconciliation detects the complete file lifecycle
The system SHALL detect and reconcile file creation, content modification, movement, renaming, and deletion within a registered workspace. It SHALL also perform a full reconciliation scan after startup or an observation gap.

#### Scenario: New file appears
- **WHEN** a supported file is created inside a workspace
- **THEN** the system records a resource, ingests its content, and emits a workspace event

#### Scenario: File is deleted outside Katsi
- **WHEN** a previously tracked file no longer exists
- **THEN** the system records an External Change, removes it from current retrieval and relationship projections, and preserves historical evidence

#### Scenario: Watcher misses events
- **WHEN** Katsi restarts or detects an observation gap
- **THEN** a full reconciliation scan converges the Living Model to current filesystem state

### Requirement: Logical resource identity survives unambiguous moves
The system SHALL separate logical resource identity from current path. An unambiguous observed move or rename SHALL preserve resource identity and history. When a move cannot be distinguished from deletion plus creation, the system SHALL represent the ambiguity instead of silently assigning identity.

#### Scenario: Watched rename is unambiguous
- **WHEN** the observer reports a rename of a tracked file within the same workspace
- **THEN** the resource keeps its identity and history while its current path changes

#### Scenario: Duplicate content makes move inference ambiguous
- **WHEN** reconciliation finds multiple equally plausible move candidates with the same content hash
- **THEN** the system records deletion and creation or an explicit ambiguity rather than merging identities automatically

### Requirement: Content enrichment is reused exactly once per content hash
The system SHALL cache successful extraction, summary, and semantic enrichment by content hash and compatible enrichment configuration. Unchanged content and content returning to a previously enriched hash MUST NOT invoke the local model again unless the configured enrichment contract or model identity changed.

#### Scenario: Same bytes appear at a second path
- **WHEN** a supported file has a content hash already enriched under the active configuration
- **THEN** the system reuses cached enrichment without another local-model call

#### Scenario: File changes and later returns to an old hash
- **WHEN** a tracked resource returns to a previously enriched content hash
- **THEN** the system restores the cached enrichment for that hash without re-summarization

#### Scenario: Enrichment configuration changes
- **WHEN** the extraction schema or configured model identity changes incompatibly
- **THEN** the system may produce a new enrichment version while retaining prior provenance

### Requirement: Failed enrichment cannot become current semantic state
The system SHALL validate local extraction against the strict Extraction contract, retry once after an invalid response, and record an error after the second failure. Invalid or partial enrichment MUST NOT enter current graph or retrieval projections.

#### Scenario: Both extraction attempts are invalid
- **WHEN** the local model returns invalid Extraction data twice
- **THEN** the resource is marked in error and no invalid semantic projection is published

### Requirement: Current projections replace stale file semantics
After a file changes successfully, the system SHALL replace that resource's current chunks, entities, topics, and references rather than accumulating relationships from previous content. Deleted resources MUST NOT appear in current search, related-file results, or Workspace Briefs.

#### Scenario: File stops mentioning an entity
- **WHEN** changed content no longer supports a previous entity relationship
- **THEN** the old current relationship is removed while historical evidence remains available

#### Scenario: Deleted file is queried
- **WHEN** a query would previously have matched a now-deleted file
- **THEN** the current retrieval result excludes that file

### Requirement: Invalidation follows declared dependencies
The system SHALL invalidate Claims, Workspace Brief material, and proposed Change Sets only when they depend on a changed resource, invariant, or derived relationship. Unrelated External Changes MUST NOT invalidate independent work.

#### Scenario: Relevant dependency changes
- **WHEN** a resource named in a Claim or Change Set dependency changes hash
- **THEN** the dependent state is marked invalid or stale with the triggering event

#### Scenario: Unrelated file changes
- **WHEN** a file outside a Claim or Change Set's dependency closure changes
- **THEN** the independent state remains current

### Requirement: Projection failures are visible and recoverable
If a graph or vector projection update fails, the authoritative workspace event and current file state SHALL remain committed. The system SHALL expose projection lag or failure and SHALL be able to rebuild projections from authoritative state without re-summarizing unchanged content.

#### Scenario: Vector projection fails
- **WHEN** current file state commits but the vector projection cannot update
- **THEN** the system reports the projection as behind and retries or rebuilds it without losing the authoritative change event

