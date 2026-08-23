## Purpose

Give existing image resources thumbnails and OCR text through owner-supplied
local executables, without katsi importing a vision or OCR library and without
weakening its offline execution guarantee.

## ADDED Requirements

### Requirement: Owner-supplied executables satisfy the strict contracts
The system SHALL obtain image understanding only from owner-configured local
executables bound through `MediaPipelineDefinition`. It SHALL NOT import a
vision or OCR library, and it SHALL treat output that violates a declared
contract as a failure rather than repairing it.

#### Scenario: OCR wrapper emits the declared JSON
- **WHEN** the configured OCR executable runs against an image
- **THEN** it writes a JSON document containing a required `text` key, and the system records an `ocr_text` representation

#### Scenario: Wrapper emits malformed output
- **WHEN** a configured executable writes output that does not satisfy its declared contract
- **THEN** the system records the failure and preserves any existing representation, rather than storing partial or repaired content

#### Scenario: Configured executable is absent
- **WHEN** a pipeline names an executable that is not present on the host
- **THEN** the system reports that pipeline unavailable and continues processing other resources

### Requirement: Image pipelines run without network access
Configured image pipelines SHALL execute with network access denied. The system
SHALL NOT require a pipeline to reach a network service, including one listening
on loopback.

#### Scenario: Pipeline attempts a loopback connection
- **WHEN** a configured image pipeline attempts to reach a service on localhost
- **THEN** the connection is denied by the platform isolation mechanism, and the pipeline is expected to fail rather than silently degrade

### Requirement: Semantic description of image content is out of scope here
The system SHALL NOT produce captions or other free-text semantic descriptions
of image content through this configuration. A consumer that needs semantic
judgement SHALL derive it outside katsi from the evidence katsi provides.

#### Scenario: Consumer needs a subject description
- **WHEN** a consumer requires a semantic description of what an image depicts
- **THEN** it derives that from OCR text and other katsi evidence on its own side, and katsi records no caption representation

### Requirement: Reprocessing is repeatable and non-destructive
Re-running image understanding SHALL reuse work whose fingerprint is unchanged,
SHALL create a new current representation when the configured policy changes,
and SHALL preserve superseded representations as historical provenance.

#### Scenario: Unchanged configuration is re-run
- **WHEN** image understanding runs again with an unchanged executable, arguments and sampling policy
- **THEN** the existing representations are reused and the executables are not invoked again

#### Scenario: OCR language changes
- **WHEN** the configured OCR language changes
- **THEN** the system produces a new current representation and retains the prior one as history
