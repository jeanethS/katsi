# Multimedia Understanding — Handoff to Codex

## What this is

OpenSpec change `multimedia-understanding` at `openspec/changes/multimedia-understanding/`. Full spec in `specs/multimedia-understanding/spec.md`, design rationale in `design.md` (16 numbered decisions), task breakdown in `tasks.md` (14 sections, 98 tasks).

Goal: replace Katsi's "every file becomes one text string" assumption with modality-neutral Derived Representations that carry precise spatial/temporal provenance (page, bounding box, timestamp) through retrieval and Claims, processed locally through bounded, owner-configured adapters.

## Current state: sections 1–8 done, sections 9–14 pending

55 of 98 tasks checked off (`[x]` in tasks.md). Only task 3.1 (hardware benchmarking — needs a real machine run, can't be done by an agent) is unchecked within sections 1–8; everything else in 1–8 is implemented and tested.

**Nothing is committed.** All new/modified files are sitting in the working tree, uncommitted, ready for review. Run `git status` before doing anything destructive.

### Files added (all untracked, `packages/core/katsi_core/media/`)

| File | Section | Purpose |
|---|---|---|
| `contracts.py` | 1 | Pydantic models: `DerivedRepresentation`, `MediaRepresentationKind`, discriminated `EvidenceLocatorUnion` (7 locator types), `MediaPipelineDefinition`, `MediaProcessingConfig`, `PipelineFingerprint` |
| `protocols.py` | 3 | `MediaDetectorProtocol`, `MediaPipelineProtocol`, `LazyAdapterLoader`, availability probing |
| `detection.py` | 3 | Content-signature detection (magic numbers for PNG/JPEG/GIF/BMP/WebP/TIFF/PDF/WAV/AVI/MP3/FLAC/OGG/Matroska/MP4-family, plus zip-based DOCX/PPTX/XLSX); extension-mismatch and encrypted/malformed reporting |
| `pipeline_registry.py` | 3 | Owner-authored `MediaPipelineDefinition` registry; `resolve(mime_type, kind)` is the only agent-facing selection surface |
| `execution.py` | 3 | `BoundedSubprocessExecutor` — the only place any subprocess is ever spawned for media processing. `shell=False` always, fixed 3-placeholder template substitution (`ALLOWED_ARG_PLACEHOLDERS`), sanitized env, private tempdir, timeout/output bounds, best-effort network isolation |
| `registry.py` | 2 | `RepresentationRegistry` — authoritative, immutable, versioned storage with lifecycle transitions (pending/current/partial/unavailable/failed) |
| `blob_store.py` | 2 | Content-addressed (blake3) private blob store for large binaries, dedup, atomic writes |
| `planner.py` | 4 | `PipelineDAGPlanner` (wave-based topological planning), `StageRunner` (idempotency/retry/cancellation), `ConcurrencyLimiter` (CPU/GPU/media caps), `aggregate_coverage()` |
| `fingerprint.py` | 4 | `build_pipeline_fingerprint()` — blake3 hash over deterministic sorted-key JSON, **includes `MediaSamplingSettings.get_fingerprint_components()`** so chunking-policy changes correctly invalidate cache (see Decision 16 in design.md) |
| `cache.py` | 4 | `RepresentationCache` — fingerprint-digest-based compatible lookup, works across resource-version copies (A→B→A reuse) |
| `image_metadata.py` | 5 | Deterministic image inspection: dimensions, color mode/alpha, hand-rolled TIFF/EXIF IFD reader for orientation + GPS presence. **EXIF GPS is privacy-gated** — `include_privacy_fields=False` by default, GPS never lands in the default METADATA representation |
| `image_pipeline.py` | 5 | Four independent adapters: thumbnail, OCR, caption, visual embedding — all via `BoundedSubprocessExecutor`, none depends on another's output |
| `document_pipeline.py` | 6 | PDF text-sufficiency evaluation, bounded page rendering (poppler/pdftoppm via subprocess), OCR reuse through `MediaPipelineRegistry.resolve()` (not a direct import of Section 5), native-vs-OCR dedup via `difflib` |
| `audio_pipeline.py` | 7 | WAV metadata parsing (pure in-process, no subprocess), decode/transcription/speaker-segmentation as subprocess adapters, transcript chunking as a **strict partition** (not overlap — overlap would duplicate Claim evidence for time-based segments) |
| `video_pipeline.py` | 8 | `VideoCoveragePlanner` (the critical piece — pre-decode budget planning, never truncates to a silent prefix), scene detection with max-interval fallback, keyframe extraction, integration glue to audio_pipeline/image_pipeline |

Also modified (previously committed, now dirty): `config.py` (added `MediaSamplingSettings`/`ChunkingThresholds`), `ingest/chunk.py` (now takes `settings: ChunkingThresholds` instead of hardcoded params).

Corresponding test files: `tests/test_media_{contracts,registry,blob_store,detection,pipelines,fingerprint,planner,cache,image_pipeline,document_pipeline,audio_pipeline,video_pipeline}.py`. **323 tests, all passing.**

### Verify before touching anything

```bash
source .venv/bin/activate
python -m pytest tests/test_media_contracts.py tests/test_media_registry.py tests/test_media_blob_store.py \
  tests/test_media_detection.py tests/test_media_pipelines.py tests/test_media_fingerprint.py \
  tests/test_media_planner.py tests/test_media_cache.py tests/test_media_image_pipeline.py \
  tests/test_media_document_pipeline.py tests/test_media_audio_pipeline.py tests/test_media_video_pipeline.py -q
# expect: 323 passed

ruff check packages/core/katsi_core/media/ tests/test_media_*.py
# expect: All checks passed!
```

## Known pre-existing failures (not caused by this work, don't try to fix as part of sections 9+)

`tests/test_media_benchmark_{probes,harness,scoring,report}.py` — 7 failures. These belong to `benchmarks/media/` (a separate local-hardware-adapter-benchmark harness built for task 3.1, unrelated to the openspec sections 1–8 implementation). Confirmed via `git stash` by one of the section agents that these predate this session's work. Leave them alone unless the user asks you to pick up task 3.1.

## Reconciliation items — worth a look before or during section 9+

These are things agents flagged as assumptions that haven't been cross-checked against reality yet:

1. **`document_pipeline.py`'s `DocumentOcrCoordinator`** expects OCR output locators to be either `ImageRegionLocator` or `WholeResourceLocator`. It resolves the OCR-producing pipeline for image MIME types via `MediaPipelineRegistry.resolve()`, then remaps to one-based `PageLocator`. `image_pipeline.py`'s `ImageOcrPipeline` does produce one of those two locator types (confirmed compatible by the image agent), so this should already be fine — but worth a real integration test that registers both pipelines together and runs a hybrid PDF through the full path, since so far each section's tests exercise its own pipeline in isolation with mocked adapters.

2. **`audio_pipeline.py`'s decode stage (7.2)** does not wire into `blob_store.py` — it stores a content-addressed marker string instead of an actual blob reference. Every other pipeline that produces a binary artifact (image thumbnails, document page renders, video audio-track/keyframe extraction) does use `BlobStore.store_blob()`. This is an inconsistency worth fixing: either wire audio decode into `BlobStore` for consistency, or confirm there's a reason it's different (the agent's stated reason was "avoided touching blob_store.py to reduce collision risk with concurrent sections" — that risk is gone now that all sections have landed).

3. **`video_pipeline.py`'s integration with Sections 5 and 7** guessed a bytes-in/bytes-out Protocol shape before those sections existed, then had to add `*_via_audio_pipeline`/`*_via_image_pipeline` glue functions once the real file-path-based `MediaPipelineProtocol` adapters landed. The original guessed Protocols (`AudioTranscriptionAdapter`, `ImageCaptionAdapter`, `ImageEmbeddingAdapter`) are still in the module as the "documented abstract shape" used by its own unit tests, alongside the real glue. Worth deciding whether to collapse these to one shape or keep both (probably fine to keep — the glue functions are the real integration path, the abstract protocols are just for unit-testing `video_pipeline.py` without spinning up ffmpeg).

4. **`registry.py` had a real bug, now fixed**: `find_cached_representation`/`get_representations_by_pipeline` were serializing pipeline fingerprints with python-mode `model_dump()` on lookup but json-mode `model_dump(mode="json")` on insert — this would crash outright (`TypeError`, raw `UUID` isn't JSON-serializable) whenever a fingerprint had `input_representation_id` set. Fixed, with a regression test (`test_find_cached_representation_with_input_representation_id`). No further action needed, just flagging that it happened in case similar model_dump-mode mismatches exist elsewhere in newer pipeline files — worth a quick grep: `grep -rn "model_dump()" packages/core/katsi_core/media/*.py` and check each one is intentional python-mode, not a copy-paste of a json-mode-serialized field.

5. **`MediaPipelineDefinition` gained `stage: PipelineStage` (required) and `input_kinds: list[MediaRepresentationKind]` fields** partway through the session (added to reconcile a gap `planner.py`'s Section 4 agent had worked around with a parallel local type). All 5 existing construction sites were updated. If sections 9+ or any new code constructs `MediaPipelineDefinition` directly, it now needs `stage=` — this is enforced by Pydantic (no default), so it'll fail loudly if missed, not silently.

## What's next: sections 9–14

```
## 9. Modality-Aware Vector and Graph Projections       (9.1–9.7,  7 tasks)
## 10. Context Assembly and MCP Retrieval Surface        (10.1–10.7, 7 tasks)
## 11. Privacy, Untrusted-Content, and Capability Gating  (11.1–11.6, 6 tasks)
## 12. Governed Derived-Media Operations                 (12.1–12.6, 6 tasks)
## 13. Migration and Backward Compatibility               (13.1–13.6, 6 tasks)
## 14. Validation, Benchmarks, and Documentation           (14.1–14.6, 6 tasks)
```

Full task text is in `openspec/changes/multimedia-understanding/tasks.md`. Read the corresponding design.md decisions before starting each section — in particular:

- **Section 9** depends on `agentic-workspace-coordination`'s projection interfaces (a separate, sibling OpenSpec change — read that change's design.md too) and on Decision 11 ("Keep embedding spaces separate and fuse evidence explicitly") — do not insert visual vectors into the existing text table or compare raw cosine distances across incompatible embedding spaces.
- **Section 10** depends on Decision 12 ("Assemble context from bounded previews and locator-backed text") — raw media bytes, base64 payloads, full transcripts, and full-resolution images are excluded from context by default; a capability-checked operation retrieves the cited preview separately.
- **Section 11** depends on Decision 13 ("Treat media content as untrusted evidence") — OCR/captions/transcripts/filenames/metadata can never select pipelines, alter policy, or activate intent. This section is security-sensitive; don't rush it.
- **Section 12** depends on Decision 14 ("Derived media operations extend the closed Change Set algebra") — again reads `agentic-workspace-coordination`'s Change Set interfaces.
- **Section 13** is migration/compat — read the "Migration Plan" section at the bottom of design.md (6 numbered steps) before touching this.
- **Section 14** is validation/docs/benchmarking — do this last, it depends on everything else being real.

## How this session worked (for continuity, not a requirement to repeat)

Sections were built by parallel Sonnet subagents, one per section, each given: the relevant tasks.md section, pointers into design.md, the existing contracts/patterns to reuse (never reimplement `BoundedSubprocessExecutor`, never call OCR/vision/audio/video libraries directly — always go through the pipeline registry + orchestrator), and instructions to build minimal local Protocol stand-ins if a dependency from a concurrently-running section wasn't there yet, then flag it for reconciliation. That's why item 3 above exists — it's the expected shape of running sections in parallel, not a mistake.

If continuing with the same approach for sections 9–14: sections 9 and 10 are more tightly coupled to each other (and to the sibling `agentic-workspace-coordination` change) than 1–8 were to each other, so parallelizing them may produce more reconciliation debt than it saves. Consider doing 9 and 10 sequentially, or at least reading both task lists before splitting.

## Everything else you need is in the repo

- `openspec validate multimedia-understanding --strict` should pass — run it after any tasks.md edits.
- User's global CLAUDE.md conventions apply: ruff line-length 100, rules E,F,I,UP,B,SIM,N, `from __future__ import annotations`, no new third-party deps in `katsi_core` core (heavy libs stay behind the optional-adapter/lazy-loading boundary per Decision 15), tests use tiny synthetic in-test fixtures rather than external binaries.
- Don't commit unless explicitly asked. Don't push. Don't touch `.claude/worktrees/multimedia-understanding/` (a stale worktree with a divergent older copy of some of these files — noted early in the session, never resolved, probably safe to `git worktree remove` after confirming nothing there is needed, but that's a call for whoever's driving, not an automatic cleanup).
