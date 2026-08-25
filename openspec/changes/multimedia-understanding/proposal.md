## Why

Katsi currently reduces supported files to plain text, so images, screenshots, audio, video, and scanned documents are opaque or lose the location and modality needed for trustworthy agent evidence. A persistent workspace model must understand these common project artifacts locally and cite precise regions or time ranges without modifying originals.

## What Changes

- Recognize image, audio, video, and scanned-document resources by validated media type rather than extension alone.
- Produce local, content-hash-cached Derived Representations such as metadata, OCR, captions, transcripts, timestamped segments, scene boundaries, and sampled keyframes.
- Add precise Evidence Locators for pages, regions, timestamps, scenes, and frames so agents can inspect the source behind a Claim.
- Add modality-aware retrieval that searches textual and visual representations separately, fuses results at the resource level, and returns compact locator-backed evidence.
- Route configured local media types through those pipelines from `katsi index`, reporting unavailable media without falling back to text extraction.
- Add an optional macOS `sips` adapter for HEIC detection and private PNG thumbnails.
- Support partial media-processing success and expose unavailable or failed representations without poisoning valid results.
- Add owner-configured media pipeline definitions, resource budgets, privacy controls, and provenance fingerprints.
- Allow governed creation and replacement of derived media artifacts while prohibiting destructive mutation of original media and agent-generated arbitrary processing commands.

## Capabilities

### New Capabilities

- `multimedia-understanding`: Local extraction, representation, retrieval, citation, and safe derived-artifact handling for image, audio, video, and scanned-document resources.

### Modified Capabilities

None.

## Impact

- `katsi_core` gains modality-neutral resource representations, evidence locators, local media-pipeline adapters, and media-aware retrieval fusion.
- Text-only `Chunk` and `Extraction` behavior remains supported but becomes one representation path rather than the universal file model.
- LanceDB may use separate modality-compatible tables or indexes; Kùzu relates resources, representations, topics, entities, scenes, and evidence without making projections authoritative.
- Configuration gains supported media types, local model and tool identities, sampling rules, timeouts, size limits, and privacy policy.
- MCP context and search results gain typed representation and locator metadata while remaining budget-capped.
- Optional local media dependencies are isolated behind adapters and faked or fixtured in CI.
