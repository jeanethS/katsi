## Purpose

Allow existing media resources to gain configured local derived
representations without deleting or recreating their workspace index.

## ADDED Requirements

### Requirement: Owner configuration enables local media adapters
The application SHALL expose media processing configuration through
`katsi.toml`. It SHALL bind a configured pipeline only to a fixed local adapter
allowlist and SHALL require the owner to provide every executable path, model
identity, and enabled media family. It MUST NOT invent, download, or select an
executable or remote service.

#### Scenario: Owner enables a configured transcription adapter
- **WHEN** the owner configures and enables a supported local transcription adapter with a valid executable path
- **THEN** the CLI registers that adapter and may use it for eligible media resources

#### Scenario: No media adapter is configured
- **WHEN** the owner has not configured an enabled adapter for a media resource
- **THEN** the CLI reports the resource as unavailable and does not attempt a semantic pipeline

### Requirement: Explicit media reprocessing preserves indexed state
The CLI SHALL provide an explicit media-reprocessing mode for a file or
directory already tracked in a workspace. It SHALL retain the source resource
version, existing text index, workspace history, and prior derived
representations.

#### Scenario: Reprocess an existing media directory
- **WHEN** a user invokes the media-reprocessing mode for an already registered media directory
- **THEN** the system processes the current media resources without requiring a new workspace or deleting existing index data

### Requirement: Reprocessing runs eligible configured pipelines
The media-reprocessing mode SHALL evaluate each media resource against the
currently configured and available local pipeline policy, and SHALL create
missing or fingerprint-incompatible derived representations. It SHALL report
unavailable resources without failing the remaining run.

#### Scenario: Video gains a newly enabled scene pipeline
- **WHEN** an existing video has no compatible scene representation and an eligible scene pipeline is available
- **THEN** the system creates the scene representation and reports the resource as processed

#### Scenario: Required pipeline is unavailable
- **WHEN** an existing media resource requires a pipeline that is unavailable
- **THEN** the system preserves the existing representations, reports the resource as unavailable, and continues processing other resources

### Requirement: Reprocessing reuses compatible representations
The media-reprocessing mode SHALL reuse a successful representation with a
compatible source content hash and pipeline fingerprint rather than executing
the pipeline again. It SHALL preserve older representations when an
incompatible fingerprint produces a replacement.

#### Scenario: Compatible representation already exists
- **WHEN** a media resource has a compatible current representation
- **THEN** the system reports it as reused and does not rerun the local pipeline

#### Scenario: Pipeline policy changes
- **WHEN** the configured pipeline fingerprint is incompatible with an existing representation
- **THEN** the system creates a new current representation while retaining the prior representation as historical provenance

### Requirement: Reprocessing preserves sibling representations
The system SHALL atomically register all representations produced for one media
resource in a processing run. It MUST keep distinct current scene, keyframe,
transcript, silence, and region representations visible rather than allowing a
later sibling insert to replace an earlier sibling.

#### Scenario: A video produces multiple scenes
- **WHEN** one video processing run produces several scene representations
- **THEN** all scenes remain current and addressable after the run completes
