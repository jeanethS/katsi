## 1. Integration Contracts and Configuration

- [x] 1.1 Reconcile and lock the Resource Version, Claim evidence, projection, and governed-operation integration contracts with `agentic-workspace-coordination` before implementation.
- [x] 1.2 Add strict enums and Pydantic models for media descriptors, representation kinds/statuses, coverage, producer provenance, pipeline fingerprints, and representation errors.
- [x] 1.3 Add a discriminated Evidence Locator union for whole resource, text range, document page/region, image region, audio time range, video frame, and scene.
- [x] 1.4 Add configuration models for supported MIME patterns, pipeline catalog, model/tool identities, language policy, sampling, privacy classes, timeouts, memory/output limits, and modality feature flags.
- [x] 1.5 Add validation and JSON round-trip tests for every representation and locator variant, including invalid coordinates, timestamps, coverage, and provenance.
- [x] 1.6 Preserve existing `Chunk`, `Extraction`, and text-only configuration compatibility while exposing conversion into the representation model.

## 2. Representation Registry and Blob Storage

- [x] 2.1 Implement the authoritative Derived Representation registry behind a core adapter with immutable versions and current-status selection.
- [x] 2.2 Implement independent pending, current, partial, unavailable, and failed lifecycle transitions with bounded structured errors.
- [x] 2.3 Implement a private content-addressed derived-blob store with hash verification, deduplication, atomic writes, and configured retention metadata.
- [x] 2.4 Store large binaries by private blob reference and reject unbounded inline binary payloads in representation contracts.
- [x] 2.5 Implement source-resource deletion/change handling that preserves historical representations while removing non-current projection visibility.
- [x] 2.6 Add tests for immutable replacement, blob deduplication, corrupted blobs, partial coverage, and source-version provenance.

## 3. Media Detection and Pipeline Registry

- [x] 3.1 Benchmark candidate local detectors, OCR, transcription, caption, scene-detection, and visual-embedding adapters on supported platforms and record selected defaults in the design/configuration.
- [x] 3.2 Define detector and media-pipeline protocols in core with lazy optional-adapter loading and availability probes.
- [x] 3.3 Implement content-signature/container detection with extension mismatch reporting, deterministic metadata, and unsupported/encrypted/malformed states.
- [x] 3.4 Implement the owner-configured pipeline registry with accepted inputs, produced representations, fixed executable/model identity, environment policy, and resource budgets.
- [x] 3.5 Implement bounded subprocess execution with `shell=False`, sanitized environment, private temporary directories, timeout/output enforcement, and network-disabled policy where supported.
- [x] 3.6 Implement strict model-output validation with one retry and failed status after the second invalid result.
- [x] 3.7 Add fake detector/pipeline adapters and tests proving agent-supplied commands never execute.

## 4. Pipeline DAG and Content-Hash Cache

- [x] 4.1 Implement a representation DAG planner using declared input/output kinds and independent branch status.
- [x] 4.2 Implement a complete pipeline fingerprint covering source hash, input representation, adapter/contract version, model/tool identity, prompt, language, sampling, and normalization policy.
- [x] 4.3 Implement compatible cache lookup so copied media and A→B→A histories reuse successful representations without repeating expensive work.
- [x] 4.4 Implement stage-level idempotency, retry, cancellation, and coverage aggregation without marking partial resources fully understood.
- [x] 4.5 Implement configured global and per-workspace concurrency limits for CPU/GPU/media jobs.
- [x] 4.6 Add tests proving sibling stages survive independent failures and incompatible fingerprint changes produce new representation versions.

## 5. Image and Screenshot Understanding

- [x] 5.1 Implement deterministic image metadata extraction for dimensions, orientation, color/alpha properties, and classified metadata fields.
- [x] 5.2 Implement private orientation-normalized thumbnails/previews without altering original bytes.
- [x] 5.3 Implement whole-image and region-aware local OCR with normalized bounding-box locators and confidence metadata.
- [x] 5.4 Implement optional local image captioning through a configured vision adapter and strict caption contract.
- [x] 5.5 Implement optional visual embedding generation through a configured compatible encoder.
- [x] 5.6 Keep OCR, captions, metadata, thumbnails, and embeddings independent so any valid subset remains usable.
- [x] 5.7 Add tiny fixtures and tests for screenshots, photographs without text, rotated images, transparent images, malformed input, and EXIF location privacy.

## 6. Scanned-Document Understanding

- [x] 6.1 Implement configured document text-sufficiency evaluation using page count, extracted coverage, and image-only page evidence.
- [x] 6.2 Implement bounded local page rendering for pages requiring OCR without replacing the source document.
- [x] 6.3 Reuse the image OCR pipeline and attach one-based page plus normalized-region locators to recognized text.
- [x] 6.4 Keep native extracted text and OCR distinguishable and add deduplication evidence for overlapping passages.
- [x] 6.5 Record unavailable states for encrypted, password-protected, oversized, or unrenderable documents.
- [x] 6.6 Add fixtures and tests for text-native, image-only, hybrid, rotated-page, encrypted, and partial-failure documents.

## 7. Audio Understanding

- [x] 7.1 Implement deterministic audio metadata extraction for container, codec, duration, channels, and sample rate.
- [x] 7.2 Implement bounded local decoding to a private normalized working representation.
- [x] 7.3 Implement configured local speech transcription returning strict timestamped segments and coverage.
- [x] 7.4 Implement transcript chunk assembly that preserves source segment locators and prevents duplicate overlapping Claim evidence.
- [x] 7.5 Implement optional anonymous speaker segmentation without real-world voice identity inference.
- [x] 7.6 Represent silence, music, unsupported language, and unrecognized speech without fabricating transcript text.
- [x] 7.7 Add short deterministic fixtures and tests for mono/stereo audio, multiple speakers, silence, partial duration, decoder failure, and cache reuse.
- [x] 7.8 Implement MediaSamplingSettings with configurable ChunkingThresholds (target_tokens, overlap, separator_hierarchy) as part of the pipeline fingerprint.
- [x] 7.9 Bind chunking policy changes to representation versioning so that different target_tokens or overlap values produce new representation versions instead of silently reusing cached chunks.
- [x] 7.10 Update existing chunk implementation to use MediaSamplingSettings instead of hardcoded parameters and ensure chunking thresholds are included in PipelineFingerprint computation.

## 8. Video Understanding

- [x] 8.1 Implement deterministic video metadata extraction for streams, codecs, dimensions, duration, frame rate, and audio availability.
- [x] 8.2 Implement a pre-decode coverage planner enforcing duration, keyframe, pixel, output-byte, wall-time, and compute budgets.
- [x] 8.3 Implement local audio-track extraction and reuse the audio transcription pipeline with original video time locators.
- [x] 8.4 Implement configured scene-boundary detection with maximum-interval fallback sampling.
- [x] 8.5 Implement private keyframe extraction with frame/time locators and source-scene relationships.
- [x] 8.6 Implement optional keyframe captions and visual embeddings through the image pipeline adapters.
- [x] 8.7 Implement scene representations combining scene range, selected keyframes, and overlapping transcript evidence.
- [x] 8.8 Add fixtures and tests for silent video, speech plus slides, variable frame rate, oversized video, partial coverage, scene failure, and interrupted processing.

## 9. Modality-Aware Vector and Graph Projections

- [x] 9.1 Extend text projection to index OCR, captions, and transcript chunks with representation and locator metadata.
- [x] 9.2 Implement separate visual vector tables/indexes keyed by compatible embedding-space fingerprint and dimension.
- [x] 9.3 Add query routing for text-to-text, configured cross-modal text-to-visual, and capability-checked image-to-visual searches.
- [x] 9.4 Implement per-space score calibration and resource-level fusion with explicit modality/evidence contributions.
- [x] 9.5 Extend graph projection to relate resources, representations, pages, scenes, keyframes, transcript segments, entities, topics, and Claim evidence.
- [x] 9.6 Implement replacement/removal of current media projections when source or representation versions change.
- [x] 9.7 Add tests proving incompatible embeddings are never compared directly and projection rebuild reuses cached representations.

## 10. Media-Aware Search, Context, and Claims

- [x] 10.1 Extend search hits with resource id, representation kind/status, Evidence Locator, coverage, provenance, and per-signal relevance evidence.
- [x] 10.2 Group representation hits by source resource and prevent one long recording or video from monopolizing the result budget.
- [x] 10.3 Extend context assembly with bounded OCR/caption/transcript previews, page/time/region citations, and optional small thumbnail references.
- [x] 10.4 Exclude complete transcripts, full-resolution images, base64 media, and raw media bytes from default context bundles.
- [x] 10.5 Add capability-checked operations for retrieving a cited preview or opening the original resource separately.
- [x] 10.6 Allow Claims to cite representation-plus-locator evidence and invalidate verification when the source or representation becomes non-current.
- [x] 10.7 Add MCP contract tests for image, scan, audio, and video results under strict token/media budgets.

## 11. Privacy and Evidence Boundaries

- [x] 11.1 Add configurable sensitive metadata classifications for location and biometric-like media outputs.
- [x] 11.2 Require matching Capability Grants before sensitive metadata can enter Workspace Briefs, search previews, or remote-capable surfaces.
- [x] 11.3 Mark OCR, captions, transcripts, subtitles, filenames, and metadata as untrusted evidence that cannot select pipelines, change intent, or authorize actions.
- [x] 11.4 Ensure prompts and adapter contracts delimit media-derived content as data and never execute instructions extracted from media.
- [x] 11.5 Prohibit face identity, voice identity, emotion inference, and remote upload in the initial pipeline catalog.
- [x] 11.6 Add tests for EXIF location redaction, prompt injection in OCR/transcripts, unauthorized preview access, and network-call denial.

## 12. Governed Derived-Media Operations

- [x] 12.1 Add strict Change Set operation variants for thumbnail generation, transcript/OCR export, keyframe export, proxy generation, representation export, and exact-hash derived-artifact replacement.
- [x] 12.2 Require every operation to reference a registered pipeline, immutable source version, expected output media type, limits, and source relationship.
- [x] 12.3 Stage and validate output hash/media descriptor before committing a new derived workspace artifact.
- [x] 12.4 Journal source, pipeline fingerprint, output, verification, and rollback evidence through the governed executor.
- [x] 12.5 Reject in-place lossy transcoding, metadata stripping of originals, destructive original edits, publishing, uploading, and arbitrary processing commands.
- [x] 12.6 Add idempotency, rollback, failure-injection, and original-hash-preservation tests for every derived-media operation.

## 13. Migration and Compatibility

- [x] 13.1 Import existing extracted text and chunks as text representations with migration provenance and no changed retrieval behavior.
- [x] 13.2 Add configured media include patterns only when their required detector/pipeline availability probes pass.
- [x] 13.3 Reconcile existing media files into metadata/unavailable states before enabling expensive semantic stages.
- [x] 13.4 Preserve text-only installation and startup when no optional media extras are installed.
- [x] 13.5 Implement feature-level rollback that disables media pipelines and visual indexes without deleting representation manifests or blobs.
- [x] 13.6 Add migration tests proving old binaries ignore but do not destroy new private representation state.

## 14. Dogfood, Benchmarks, and Documentation

- [x] 14.1 Build a small licensed fixture corpus covering screenshots, diagrams, scans, speech, multi-speaker audio, slides, silent video, and speech-plus-visual video.
- [ ] 14.2 Benchmark selected local adapters for accuracy, locator quality, latency, peak memory, disk use, and hardware fallback.
- [x] 14.3 Measure representation reuse, partial coverage, per-modality retrieval quality, score calibration, and context cost.
- [x] 14.4 Dogfood a cross-modal Claim that cites an image region, PDF page, audio interval, and video keyframe from the Katsi workspace.
- [x] 14.5 Update README, configuration examples, optional dependency instructions, privacy documentation, MCP result contracts, and media troubleshooting guide.
- [x] 14.6 Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .` and resolve every failure before enabling each modality by default.
