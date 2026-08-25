## Purpose

Let Katsi understand, retrieve, cite, and safely coordinate around multimedia project artifacts through local, provenance-backed Derived Representations while preserving original bytes.

## ADDED Requirements

### Requirement: Media type detection is evidence-based and configurable
The system SHALL identify configured image, audio, video, and scanned-document types using inspected media information and SHALL retain the detected media type, extension, size, and format metadata. An extension mismatch or unsupported type SHALL be visible rather than silently processed as another modality.

#### Scenario: Supported image is detected
- **WHEN** a file's inspected media type matches a configured supported image type
- **THEN** the system records it as an image resource and selects applicable local image pipelines

#### Scenario: HEIC image is detected on macOS
- **WHEN** a HEIC ISO-BMFF brand is inspected
- **THEN** the system identifies it as `image/heic` and, when an owner-configured local `sips` pipeline is available, may create a private PNG thumbnail without modifying the original

#### Scenario: Extension disagrees with content
- **WHEN** a file extension indicates an image but inspected content indicates another type
- **THEN** the system records the mismatch and does not trust the extension as the processing authority

#### Scenario: Media type is unsupported
- **WHEN** no configured pipeline accepts the inspected media type
- **THEN** the system preserves file metadata and reports that semantic representations are unavailable

### Requirement: CLI indexing dispatches supported media safely
`katsi index PATH` SHALL detect configured media files and route them through the applicable available local media pipelines. It SHALL NOT pass image, audio, or video files to the text extractor. Files whose required media pipelines are unavailable SHALL be reported as unavailable without preventing the rest of the index run from completing.

#### Scenario: Image folder is indexed
- **WHEN** a user indexes a folder containing a supported image and its metadata pipeline is available
- **THEN** the CLI records the media representation and reports it as indexed

#### Scenario: Media pipeline is unavailable
- **WHEN** a user indexes a supported media file but no required pipeline is available
- **THEN** the CLI reports the file as unavailable and does not invoke the text extractor

### Requirement: Original media bytes remain immutable
Multimedia understanding SHALL read original resource versions without modifying them. Derived Representations MUST be stored separately and MUST retain the exact source content hash.

#### Scenario: Image OCR completes
- **WHEN** the system extracts text from an original image
- **THEN** the original image hash remains unchanged and the OCR is stored as a separate representation

### Requirement: Derived Representations have strict provenance
Every Derived Representation SHALL include a stable identifier, source resource-version identifier, representation kind, media type, status, producer identity and version, pipeline fingerprint, creation time, confidence when applicable, and one or more Evidence Locators. Text or binary outputs SHALL be validated against the representation contract before becoming current.

#### Scenario: Caption is returned by a local model
- **WHEN** a local model produces an image caption matching the configured representation contract
- **THEN** the system stores the caption with its producer, fingerprint, confidence, source hash, and locator

#### Scenario: Model output is invalid twice
- **WHEN** a model-generated representation fails validation and the one permitted retry also fails
- **THEN** the representation is marked failed and invalid output does not enter current retrieval or graph projections

### Requirement: Evidence Locators identify the source region
The system SHALL use typed Evidence Locators appropriate to the modality, including page and optional bounding box for documents, bounding box for images, time range for audio, and time range or keyframe for video. Locator coordinates and timestamps SHALL refer to the immutable source resource version.

#### Scenario: Search matches spoken audio
- **WHEN** a query matches a transcript segment from an audio resource
- **THEN** the result identifies the source audio and the segment's start and end timestamps

#### Scenario: Claim cites part of an image
- **WHEN** an agent cites OCR or visual evidence from a region of an image
- **THEN** the Claim evidence links the source version and normalized bounding box

### Requirement: Image understanding produces independent representations
For a configured supported image, the system SHALL extract deterministic metadata and MAY produce OCR text, region-aware OCR, a local caption, a thumbnail, and a visual embedding according to the active pipeline policy. Failure or absence of one representation MUST NOT invalidate successful independent representations.

#### Scenario: Screenshot contains readable text
- **WHEN** image OCR succeeds and captioning is unavailable
- **THEN** the OCR representation remains searchable and caption status is reported unavailable

#### Scenario: Photograph contains no useful text
- **WHEN** OCR produces no meaningful text but visual understanding succeeds
- **THEN** the system retains the visual representation without inventing OCR content

### Requirement: Audio understanding is timestamped
For configured supported audio, the system SHALL extract deterministic media metadata and SHALL be able to produce a local transcript divided into timestamped segments. Optional speaker segmentation MUST use anonymous labels unless the owner separately enables an identity capability.

#### Scenario: Audio transcription succeeds
- **WHEN** the local transcription pipeline completes
- **THEN** each transcript segment includes source version, start time, end time, text, and producer provenance

#### Scenario: Speaker segmentation is enabled
- **WHEN** the pipeline distinguishes speakers without a separately authorized identity capability
- **THEN** it labels them anonymously and does not infer real-world identity

### Requirement: Video understanding is sampled and time-addressable
For configured supported video, the system SHALL extract media metadata and SHALL be able to derive the audio transcript, scene boundaries, sampled keyframes, keyframe captions, and visual embeddings according to configured resource budgets. It MUST NOT require decoding or embedding every frame.

#### Scenario: Video contains speech and slides
- **WHEN** transcription and keyframe extraction succeed
- **THEN** search can return independently cited transcript time ranges and visual keyframes from the same source video

#### Scenario: Video exceeds processing budget
- **WHEN** full configured processing would exceed duration, frame, byte, or compute limits
- **THEN** the system applies the configured sampling or partial-processing policy and reports the coverage achieved

### Requirement: Scanned documents preserve page location
When a document has insufficient extractable text and a configured page-rendering pipeline is available, the system SHALL be able to derive page images and page-aware OCR without replacing the original document.

#### Scenario: Image-only PDF is indexed
- **WHEN** normal text extraction produces insufficient content and OCR succeeds
- **THEN** the OCR text is searchable with page and region locators tied to the original PDF version

### Requirement: Multimedia processing is local by default
Metadata extraction, OCR, transcription, captioning, embedding, sampling, and representation validation SHALL run through configured local pipelines. No original or Derived Representation SHALL be sent to a remote model or service without a separate explicit owner-authorized capability.

#### Scenario: No remote capability exists
- **WHEN** a local pipeline cannot process a media resource
- **THEN** the system reports the unavailable representation rather than uploading the resource

### Requirement: Processing is cached by source and pipeline fingerprint
The system SHALL reuse successful Derived Representations for the same content hash and compatible pipeline fingerprint. The fingerprint SHALL account for representation contract version, configured producer identity, relevant model or tool version, and sampling policy.

#### Scenario: Same video appears at another path
- **WHEN** a video content hash already has compatible representations
- **THEN** the system reuses them without repeating transcription or keyframe analysis

#### Scenario: Sampling policy changes
- **WHEN** the owner activates an incompatible video sampling policy
- **THEN** the system may create a new representation version while preserving prior provenance

### Requirement: Partial processing has explicit status and coverage
Each requested representation SHALL independently report pending, current, unavailable, partial, or failed status. Partial representations SHALL include their processed coverage, and the parent media resource MUST NOT be represented as fully understood when required coverage is incomplete.

#### Scenario: Long audio is only partly transcribed
- **WHEN** processing stops after the configured duration limit
- **THEN** the transcript is marked partial with the completed time range

### Requirement: Retrieval is modality-aware
The system SHALL search textual representations with compatible text embeddings and visual representations only with compatible visual or shared-space embeddings. It MUST NOT compare raw scores from incompatible embedding spaces as though they were calibrated. Results SHALL be fused at the resource level with per-signal evidence.

#### Scenario: Text query matches an image caption
- **WHEN** a query semantically matches an image caption or OCR representation
- **THEN** the image resource may be returned with the matching representation and locator

#### Scenario: Visual retrieval is unavailable
- **WHEN** no compatible visual embedding pipeline is configured
- **THEN** the system continues text-derived media retrieval and reports visual search unavailable

### Requirement: Context returns compact playable evidence
Multimedia search and context results SHALL include resource identity, representation kind, Evidence Locator, relevance evidence, processing status, and a bounded text or thumbnail preview. They MUST NOT insert complete media binaries or entire long transcripts into the context bundle by default.

#### Scenario: Context includes a video match
- **WHEN** a video segment is relevant to a Workspace Brief or query
- **THEN** the context includes the cited time range, bounded preview, and source path without embedding the entire video or transcript

### Requirement: Claims can cite multimedia representations
An agent SHALL be able to attach a Derived Representation and Evidence Locator to a Claim. If the source resource version or representation becomes non-current, the system SHALL invalidate dependent verification while preserving historical provenance.

#### Scenario: Cited screenshot changes
- **WHEN** a verified Claim cites an image region and the current file changes to a new content hash
- **THEN** the system marks the evidence non-current and invalidates verification that depended on it

### Requirement: Media pipelines are owner-configured and bounded
Executable media tools and local models SHALL come from an owner-configured pipeline catalog. Pipelines SHALL define accepted media types, output representation kinds, resource limits, timeouts, permitted environment, and network policy. Agent-supplied arbitrary commands MUST NOT execute as media pipelines.

#### Scenario: Agent requests an unregistered media command
- **WHEN** an agent supplies a command absent from the active pipeline catalog
- **THEN** the system rejects it without execution

#### Scenario: Decoder exceeds a resource limit
- **WHEN** a media pipeline exceeds its configured time, memory, output, or sampling budget
- **THEN** the system terminates or limits it and records partial or failed status

### Requirement: Sensitive metadata is controlled
The system SHALL classify configured sensitive media metadata, including location metadata and biometric-like outputs, and SHALL exclude it from ordinary Workspace Briefs unless the requesting Agent Identity has a matching Capability Grant. Face or voice identity inference MUST NOT be enabled as part of the initial capability.

#### Scenario: Photograph contains coordinates
- **WHEN** location metadata is extracted from an image
- **THEN** it remains private and is omitted from a brief requested by an agent without the sensitive-metadata capability

### Requirement: Governed media actions create derived artifacts only
The governed action catalog MAY create thumbnails, transcripts, proxy media, keyframes, and exported Derived Representations and MAY replace an outdated derived artifact. Initial multimedia actions MUST NOT destructively edit, overwrite, metadata-strip, or lossily transcode an original in place, and MUST NOT upload or publish media.

#### Scenario: Generate a video proxy
- **WHEN** an authorized Change Set requests a configured proxy pipeline
- **THEN** the system creates a separate derived artifact linked to the original source version

#### Scenario: Agent requests in-place lossy conversion
- **WHEN** a Change Set requests destructive transcoding of an original media file
- **THEN** the system rejects the operation
