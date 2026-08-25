## Context

See [proposal.md](./proposal.md) for motivation and the [multimedia-understanding specification](./specs/multimedia-understanding/spec.md) for observable behavior.

The current ingest path assumes every useful artifact can become one text string:

- configured include globs cover selected text, code, PDF, and DOCX extensions;
- `extract_text()` delegates to MarkItDown and returns a plain string;
- `Chunk` represents only text and has no source-region locator;
- `Extraction` produces one summary plus entities, topics, and filename references;
- LanceDB contains one text-vector table;
- context results cannot identify an image region, document page, audio interval, or video keyframe.

Multimedia support must preserve the repository's local-first constraint, strict validated model contracts, content-hash reuse, configuration-driven models and thresholds, and test isolation from external services. It must also compose with the separate `agentic-workspace-coordination` change: multimedia representations attach to immutable Resource Versions, Claims cite them, and Kùzu/LanceDB remain projections rather than authority.

## Goals / Non-Goals

**Goals:**

- Replace the universal “file becomes text” assumption with modality-neutral Derived Representations.
- Preserve precise spatial, page, and temporal provenance through retrieval and Claims.
- Process media locally through configured, bounded, replaceable adapters.
- Reuse expensive outputs by content hash and pipeline fingerprint.
- Support useful partial results when one modality stage fails or exceeds budget.
- Keep text retrieval compatible while adding visual and media-derived evidence.
- Allow governed creation of derived media without destructive original mutation.

**Non-Goals:**

- Editing or generating original creative media in place.
- Uploading media or derived representations to remote services by default.
- Face recognition, voice identification, emotion inference, or biometric databases.
- Embedding every video frame or processing unbounded media.
- Pretending scores from incompatible embedding spaces are directly comparable.
- Replacing specialist media editors, digital-asset managers, or streaming platforms.
- Requiring all optional media dependencies for text-only installations.

## Decisions

### 1. Model a resource version as a set of Derived Representations

Introduce strict core models conceptually shaped as:

```text
ResourceVersion
  ├── source bytes and deterministic metadata
  ├── Representation(metadata)
  ├── Representation(extracted_text)
  ├── Representation(ocr)
  ├── Representation(caption)
  ├── Representation(transcript_segment) × N
  ├── Representation(scene) × N
  ├── Representation(keyframe) × N
  ├── Representation(thumbnail/proxy)
  └── Representation(embedding_reference) × N
```

A `DerivedRepresentation` contains:

- stable representation id;
- source resource-version id and content hash;
- representation kind and media type;
- independent status: pending, current, partial, unavailable, or failed;
- textual payload or private blob reference, never an unbounded inline binary;
- one or more typed Evidence Locators;
- producer adapter, model/tool identity, and version;
- pipeline fingerprint and contract version;
- confidence metadata where meaningful;
- coverage and error details;
- creation time and provenance.

Representations are immutable. Reprocessing creates a new version and marks which representation is current for a source/pipeline combination. Original bytes remain outside the representation registry and are never overwritten by understanding pipelines.

**Alternatives considered:**

- **Add optional OCR/transcript fields to `FileRecord`:** simple but cannot represent multiple segments, locators, producers, partial results, or representation versions.
- **Convert all media into one Markdown document:** preserves compatibility but destroys modality boundaries and exact provenance.
- **Treat every keyframe or segment as a synthetic file:** reuses current structures but invents paths, obscures source ownership, and makes lifecycle cleanup fragile.

### 2. Use a discriminated Evidence Locator union

Define locators using normalized or source-native coordinates:

- `WholeResourceLocator`;
- `TextRangeLocator` with character offsets in a representation;
- `PageLocator` with one-based page number and optional normalized bounding box;
- `ImageRegionLocator` with normalized `[x, y, width, height]`;
- `TimeRangeLocator` with integer start/end milliseconds;
- `VideoFrameLocator` with timestamp and optional decoded frame index;
- `SceneLocator` with start/end milliseconds and selected keyframe ids.

Every locator carries the immutable source resource-version id. Bounding boxes are normalized to `[0, 1]` so previews can scale without changing citations. Time uses integer milliseconds to avoid float equality problems. Producer-native coordinates remain optional provenance but are not the public citation format.

Claims and retrieval hits reference representation id plus locator; copied text alone is never enough to reconstruct evidence.

### 3. Detect media from content, with extension as a hint

Add a configured media detector adapter that inspects file signatures and container metadata without executing embedded content. Extension and operating-system MIME guesses remain hints. The detector returns a strict media descriptor containing MIME, container/codec details where available, dimensions, duration, page count, and mismatch warnings.

Supported media types are configured by MIME patterns and pipeline availability. Initial target families are common raster images, common local audio containers, common local video containers, and scanned PDFs. Exact extensions and size limits live in configuration.

Malformed, encrypted, unsupported, or password-protected media remain tracked resources with an unavailable/error representation rather than disappearing from the workspace.

### 4. Use a local pipeline registry instead of hardwired media commands

Introduce a `MediaPipelineRegistry` in core. Each owner-configured definition declares:

- accepted media descriptors;
- representation kinds produced;
- deterministic or model-backed producer type;
- executable/model identity and version source;
- fixed argument template and permitted variable inputs;
- local-only/network-disabled policy;
- timeout, memory, output-byte, duration, page, scene, and frame budgets;
- working-directory and environment policy;
- strict output contract and retry behavior;
- dependency and hardware availability probe.

Agents select a registered pipeline or request a representation kind; they never supply an executable or shell command. External tools run with `shell=False` in a private temporary working directory, with a sanitized environment and bounded output. Model-backed JSON is validated, retried once, then marked failed, matching Katsi's existing extraction discipline.

Pipeline definitions and all model names remain configuration-driven. Text-only installations load no optional media runtime until a relevant resource is processed.

**Alternative considered:** invoke one general multimodal model for every file. Rejected because deterministic metadata/OCR/transcoding tools are cheaper and more reliable, local hardware varies, and one model cannot provide every locator or codec operation safely.

### 5. Separate deterministic metadata, semantic extraction, and embeddings

Each modality pipeline is a DAG of independently cached stages rather than one all-or-nothing call.

```text
detect
  │
  ├── deterministic metadata
  │
  ├── text-bearing extraction ──▶ text chunks ──▶ text embeddings
  │
  ├── semantic captioning ──────▶ caption text ─▶ text embeddings
  │
  └── visual sampling ──────────▶ visual embeddings
```

A stage declares its input representation kinds and output contract. Downstream stages run only when prerequisites are current. Independent branches continue after a sibling failure. Coverage rolls up without claiming that a partially processed resource is fully understood.

### 6. Image processing keeps OCR and visual meaning separate

The initial image DAG supports:

1. deterministic dimensions, orientation, color/alpha, and selected metadata;
2. normalized preview/thumbnail derived in private storage;
3. whole-image and region-aware OCR with bounding boxes;
4. optional local captioning through a configured vision model;
5. optional visual embedding through a compatible local encoder.

OCR text and captions are separate representation kinds with separate confidence and locators. A caption does not become OCR, and empty OCR does not imply the image has no semantic content.

Orientation normalization happens in temporary derived pixels; original bytes and original hash remain unchanged. Sensitive EXIF fields are classified before they can enter summaries or briefs.

### 7. Audio processing uses timestamped segments

The audio DAG supports:

1. deterministic container, codec, duration, channel, and sample-rate metadata;
2. local decoding to a temporary normalized waveform;
3. local speech transcription with segment timestamps;
4. optional anonymous speaker segmentation;
5. text chunking and embedding that preserve segment boundaries.

Transcript chunks may combine adjacent short segments for retrieval efficiency, but each included segment retains its original time locator. Chunk overlap must not create duplicate Claim evidence. Speaker labels are ephemeral anonymous ids scoped to one resource unless a future separately authorized identity capability is designed.

Silence, music, or unrecognized speech produces explicit coverage/status rather than fabricated text.

### 8. Video processing is budgeted sampling, not exhaustive frames

The video DAG supports:

1. deterministic stream, dimension, duration, frame-rate, and codec metadata;
2. audio-track extraction followed by the audio DAG;
3. deterministic scene-boundary detection where configured;
4. keyframe selection at scene boundaries with a configured maximum interval fallback;
5. optional keyframe captions and visual embeddings;
6. scene representations linking transcript intervals and keyframes.

The sampling planner calculates an explicit coverage plan before decoding frames. It observes configured limits for duration, keyframes, decoded pixels, output bytes, wall time, and compute class. When the full plan exceeds budget, policy chooses bounded sampling, owner approval, or unavailable status. It never silently samples a small prefix and labels the entire video understood.

Derived keyframes and proxies are private representations by default. Export to workspace files requires a governed derived-artifact Change Set.

### 9. Scanned documents use a text-sufficiency fallback

Normal document extraction remains first. A configured text-sufficiency policy considers page count, extracted character coverage, and image-only pages. When insufficient, Katsi renders bounded page images locally and invokes the image OCR pipeline.

OCR output uses page and region locators. Existing extractable text and OCR remain distinguishable to avoid duplicated passages and to expose which evidence came from recognition. Hybrid pages may retain both representations with deduplication signals rather than destructive merging.

### 10. Cache stages by source hash and complete pipeline fingerprint

The representation cache key contains:

- source content hash;
- input representation id/version for downstream stages;
- representation kind;
- adapter and contract version;
- configured local model/tool identity;
- semantic prompt version where applicable;
- sampling, OCR-language, and coverage policy fingerprint;
- output normalization version.

Metadata and representation manifests live in the authoritative representation store. Large derived binaries use a private content-addressed blob store. Identical derived blobs deduplicate independently of paths. Failed runs are recorded for diagnostics but do not satisfy a future compatible success lookup.

This preserves “summarize/process once per compatible content hash” across copies and A→B→A file histories.

### 11. Keep embedding spaces separate and fuse evidence explicitly

Textual representations—extracted text, OCR, captions, and transcripts—use compatible text embeddings and may reuse the current text retrieval path. Native visual embeddings use a separate table/index per compatible embedding-space fingerprint and dimension.

The query planner determines available routes:

- text query to textual representations;
- text query to images/keyframes only when a configured cross-modal encoder supports text in that visual space;
- image query to visual representations only when the client supplies an authorized image query and compatible encoder;
- graph expansion from any matched representation to its resource and related resources.

Scores are normalized/calibrated within a space before fusion. Final evidence records modality, representation, raw score, calibrated contribution, and locator. The system does not insert visual vectors into the existing text table or compare unrelated cosine distances directly.

**Alternatives considered:**

- **Caption-only retrieval:** cheap and compatible but misses layout, visual similarity, and uncapturable details.
- **One universal table:** operationally simple but invalid when dimensions, models, and score distributions differ.

### 12. Assemble context from bounded previews and locator-backed text

Context assembly groups representation hits by source resource, prevents one long recording from consuming the whole budget, and favors evidence diversity. It can include:

- bounded OCR/caption/transcript text;
- page/time/region citation;
- compact metadata needed to interpret the result;
- optional small thumbnail resource reference for capable MCP clients;
- processing and coverage status;
- graph relationship sketch.

Raw media bytes, base64 payloads, complete transcripts, and full-resolution images are excluded by default. A client requests the cited preview or resource separately through a capability-checked operation.

### 13. Treat media content as untrusted evidence

OCR, captions, transcripts, filenames, subtitles, metadata, and visually encoded instructions remain evidence only. They cannot select pipelines, alter policy, grant capabilities, activate intent, or create operations. Prompts sent to local semantic models identify media-derived content as untrusted data and require strict representation-only output.

Sensitive metadata is tagged at extraction. Location metadata, possible faces, voice segments, and other configured sensitive classes are excluded from ordinary briefs and remote-capable paths unless the Agent Identity has the corresponding Capability Grant. Initial adapters do not implement real-world face or voice identification.

### 14. Derived media operations extend the closed Change Set algebra

When the governed Change Set capability is present, add typed operations that reference a registered pipeline and immutable source version:

- generate/refresh thumbnail;
- export transcript or OCR sidecar;
- extract/export selected keyframes;
- generate proxy media;
- export another current Derived Representation;
- replace an outdated derived workspace artifact with exact-hash preconditions.

The executor materializes output in staging, validates declared MIME and hash, journals the operation, and creates a separate derived artifact linked to its source. It never overwrites the original. Agent-provided command strings, in-place metadata stripping, destructive transcoding, publishing, and uploading remain outside the catalog.

### 15. Isolate optional dependencies and test at adapter boundaries

Core defines protocols and strict Pydantic contracts without importing heavy media runtimes at module import time. Modality extras provide concrete adapters. Tests use:

- tiny deterministic media fixtures committed to the test suite;
- fake detectors, OCR, transcription, caption, embedding, and sampling adapters;
- golden locator/representation JSON;
- malformed and oversized fixture cases;
- assertions that no network or external service is contacted;
- cache tests proving a compatible content hash is processed once.

Optional end-to-end local tests are explicitly marked and excluded from CI by default.

### 16. Chunking policy changes produce new representation versions

Text chunking, OCR segment assembly, transcript chunking, and other sampling policies are explicit components of the pipeline fingerprint. All configurable sampling thresholds—target token counts, overlap sizes, separator hierarchies, keyframe budgets, segment limits—live in `MediaSamplingSettings` and are included in cache key computation.

When any sampling threshold changes, the system creates a new representation version rather than reusing cached chunks. This binding prevents silent cache invalidation where increased token targets would reinterpret old evidence as if it had been chunked under the new policy.

**Alternatives considered:**
- **Version sampling policies independently:** Allows fine-grained reuse but explodes the fingerprint namespace and creates ambiguous policy interactions.
- **Treat sampling as implementation detail:** Simple but prevents owner-driven chunking strategy changes and creates the exact silent-reuse bug this decision avoids.
- **Re-chunk on every representation access:** Guarantees current policy but defeats the performance and reproducibility goals of content-addressed caching.

### 17. Keep the CLI dispatcher modality-aware

`katsi index` remains the single recursive entry point. It detects each candidate before extraction: text-compatible files use the existing `IngestPipeline`; image, audio, and video files use the configured media registry only when its availability probe passes. No media file falls through to MarkItDown. An unavailable pipeline produces an explicit unavailable result while the remaining files continue indexing. The CLI does not invent media commands or enable optional semantic stages; it only invokes owner-configured local pipelines.

### 18. Decode HEIC through an optional macOS adapter

The detector recognizes ISO-BMFF HEIC brands as `image/heic` without trusting the file extension. A separate owner-configured `/usr/bin/sips` pipeline may render a bounded PNG thumbnail in the private blob store. Its fixed arguments only accept the executor's input and output placeholders; the original remains immutable. Non-macOS installs and unavailable `sips` retain a descriptor and report the thumbnail unavailable.

## Risks / Trade-offs

- **[Media dependencies make installation large or fragile]** → Use optional modality extras, lazy adapter loading, availability probes, and text-only fallback.
- **[Malformed codecs exploit native tools]** → Process untrusted media through bounded subprocess adapters with sanitized environment, no shell, no network, private temporary directories, and patched dependencies.
- **[Video processing consumes unbounded compute and disk]** → Plan coverage first, enforce configurable frame/pixel/time/blob budgets, and expose partial coverage honestly.
- **[Captions hallucinate visual details]** → Keep captions as model-produced Claims/representations with confidence and provenance; do not treat them as OCR or verified fact.
- **[OCR and transcript errors mislead agents]** → Preserve exact locators, confidence, producer identity, and source previews so agents can inspect evidence.
- **[Embedding-score fusion produces unstable ranking]** → Calibrate per embedding space, record per-signal evidence, and avoid direct raw-score comparison.
- **[Sensitive EXIF or biometric-like data leaks into briefs]** → Classify sensitive fields, require explicit capabilities, and keep all processing local by default.
- **[Cached representations become stale after model changes]** → Include model/tool/contract/sampling fingerprints in cache keys and retain representation-version provenance.
- **[Derived sidecars clutter project folders]** → Keep representations private by default and export only through explicit governed operations.
- **[Separate OpenSpec changes drift]** → Implement multimedia against the stable Resource Version, Claim evidence, projection, and Change Set interfaces from `agentic-workspace-coordination`; reconcile the specs before either change is archived.

## Migration Plan

1. Add modality-neutral representation and locator models while preserving current `Chunk`, `Extraction`, and text retrieval behavior.
2. Add the representation registry and private derived-blob store behind adapters. Import existing extracted text and chunks as text representations with migration provenance.
3. Add content-based media detection and metadata-only processing behind disabled-by-default modality configuration.
4. Add image and scanned-document pipelines first because they require bounded static inputs and immediately support screenshots and diagrams.
5. Add timestamped audio transcription and context citation.
6. Add budgeted video audio extraction, scene sampling, and keyframe representations.
7. Add separate visual embedding indexes and calibrated multimodal fusion only after text-derived media retrieval is stable.
8. Add governed export of derived artifacts after Change Set execution is available.
9. Expand configured include patterns only when the relevant local pipeline passes its availability probe; unsupported media remains visible rather than silently skipped.

Rollback disables modality pipelines and visual indexes while retaining representation manifests and private derived blobs for later reuse. Text ingestion and retrieval continue unchanged. Older binaries ignore new private representation state and MUST NOT delete it.

## Open Questions

- The Apple M4 local benchmark is recorded in
  [`benchmarks/media/results/macos-apple-m4-2026-08-17.md`](../../../benchmarks/media/results/macos-apple-m4-2026-08-17.md).
  It selects the built-in content-signature detector and FFmpeg 8.1.2 for
  deterministic decode/sampling. OCR, transcription, captioning,
  scene-detection, and visual-embedding defaults remain disabled until their
  local adapters can be benchmarked with licensed ground-truth fixtures.
- Initial visual query input may arrive through an MCP resource reference or a workspace resource id; both map to the same capability-checked query planner.
- Representation retention defaults and thumbnail/proxy quality settings require dogfood measurements of disk use versus retrieval value.
