# Visual region extraction: where in the frame

**Date:** 2026-08-22
**Status:** Approved design, not yet implemented
**Scope:** katsi core media pipeline and graph. No consumer changes.

## Problem

katsi can answer *when* something happens in a video — scenes, keyframes,
time-coded transcript segments, measured silence spans — and it can answer
*where in the frame* for text, because OCR attaches `ImageRegionLocator`
bounding boxes to its output (`media/image_pipeline.py:418`).

It cannot answer *where in the frame* for anything that is not text. A caller
asking "which shots contain a train, and where in the frame is it" gets a
free-text caption for the whole keyframe and nothing positional.

That gap blocks two concrete things: reframing a 16:9 shot to 9:16 without
cropping the subject out, and any retrieval that needs a subject to be present
rather than merely mentioned.

## What already exists

Verified:

- `ImageRegionLocator` (`media/contracts.py:211`) with
  `bounding_box: (x, y, w, h)` normalised to 0–1, validated for bounds and
  positive extent. The type is sound and already in use.
- `KeyframeExtractionPipeline` (`media/video_pipeline.py:1283`) produces
  `KEYFRAME` representations; `SceneDetectionPipeline` (`:1089`) produces scenes.
- The bounded-subprocess pattern: an owner-authored `MediaPipelineDefinition`
  running a configured executable with `network_disabled=True` and
  `strict_output_contract=True`, never a raw `subprocess` call.
- `register_representation_batch`, which admits N representations of one kind as
  a single current generation. Without it, N regions per keyframe could not all
  be visible at once.

## The modelling problem

OCR puts *all* its regions on **one** representation as many locators. That works
for OCR because every region means the same thing: "text is here", and the text
itself lives in one payload.

Object regions are different: each region carries its own **label**, and
`ImageRegionLocator` has no label field — only a bounding box. Adding a label to
the locator would change a shared type used by OCR and by document pages, for the
benefit of one producer.

So: **one representation per detection**, each carrying exactly one
`ImageRegionLocator` and its label in the payload. That is N-per-kind, which is
precisely what `register_representation_batch` now supports.

The alternative — one representation holding a JSON array of labelled boxes —
was rejected because it makes a single detection unaddressable. A consumer could
not cite "the train at 0:04, upper-left" as evidence, which is the whole point.

## Design

### 1. New enum members

- `MediaRepresentationKind.VISUAL_REGION = "visual_region"`
- `PipelineStage.DETECT_REGIONS = "detect_regions"`

Additive. Note `tests/test_media_contracts.py::test_media_representation_kind_enum_is_complete`
asserts the exact kind set and must be updated in the same change.

### 2. No new katsi dependency

The detector is an **owner-supplied executable**, exactly like the whisper
wrapper and ffmpeg. katsi never imports a vision library, never downloads
weights, and stays offline: `network_disabled=True`.

`build_region_detect_definition(*, executable_path, labels, min_confidence=0.3,
timeout_seconds=120.0, max_output_bytes=...)` produces the definition.
`labels` is the owner's declared label set, passed to the executable and used to
validate what comes back.

This is what makes an open-vocabulary detector (OWLv2, Grounding-DINO) usable
without katsi taking a position on which one, or on what the labels mean. A
consumer that wants a closed vocabulary supplies it here; katsi enforces the
contract, not the semantics.

### 3. Strict output contract

The executable writes JSON to `output_path`:

```json
{
  "regions": [
    {"label": "train", "bounding_box": [0.12, 0.30, 0.45, 0.40], "confidence": 0.91}
  ]
}
```

Parsed by `parse_visual_regions(payload, *, allowed_labels)`, which raises rather
than repairing, consistent with every other parser in this module:

- `regions` must be a list; each entry an object.
- `label` must be a non-empty string **present in `allowed_labels`**. An
  undeclared label is a contract violation, not something to keep — otherwise the
  "closed" label set is not closed.
- `bounding_box` must be four numbers; `ImageRegionLocator`'s own validator then
  enforces normalisation and bounds. Do not pre-clamp: a detector emitting
  out-of-range boxes is misconfigured, and silently fixing it hides that.
- `confidence`, when present, must be within `[0.0, 1.0]`. Detections below
  `min_confidence` are dropped — that is filtering by a declared threshold, not
  repair.
- An empty `regions` list is valid. A keyframe containing nothing of interest is
  a real answer.

### 4. Pipeline

`VisualRegionDetectionPipeline`, modelled on the existing image pipelines:

- `accepted_mime_patterns=["image/*"]`,
  `input_kinds=[MediaRepresentationKind.KEYFRAME]` — it consumes keyframes the
  video pipeline already extracted, so it does no decoding and never touches the
  original video.
- `producer_type=MediaProducerType.MODEL_BACKED`. A detection is a model's
  opinion, not a measurement, and must not be confused with OCR's deterministic
  geometry.
- `process` returns **one** representation carrying the validated batch, matching
  `AudioTranscriptionPipeline`. The orchestrator calls `process` with exactly
  five positional arguments and caches one representation per fingerprint, so it
  cannot return a list.
- `build_visual_region_representations(regions, resource_version_id,
  pipeline_fingerprint, adapter)` expands the batch into N representations, each
  with one `ImageRegionLocator`, the label as `textual_payload`, and
  `confidence` set from the detection.

Registration uses `register_representation_batch`, so re-running detection
retires the previous generation once and admits the new one whole.

### 5. Graph projection

A `VisualRegion` node storing its box and label, and a `HAS_VISUAL_REGION`
relation from `MediaResourceVersion`:

```
CREATE NODE TABLE VisualRegion(
    id STRING, label STRING,
    x DOUBLE, y DOUBLE, width DOUBLE, height DOUBLE,
    PRIMARY KEY(id))
CREATE REL TABLE HAS_VISUAL_REGION(FROM MediaResourceVersion TO VisualRegion)
```

**Dispatch on `item.kind is MediaRepresentationKind.VISUAL_REGION`, not on
`locator_type == "image_region"`.** OCR representations also carry
`image_region` locators, so a locator-type branch would capture them and route
OCR output into `HAS_VISUAL_REGION`. This is the same trap that transcript
segments and silence spans share on `time_range`, and it needs the same
regression guard: project an OCR representation alongside a visual region and
assert the OCR one still reaches its own edge.

Storing the label on the node lets a consumer filter by label in Kùzu without a
second lookup, following the `MediaPage.number` precedent.

### 6. Which frames

Detection runs on keyframes only, never on every frame. §1.4 of the Brand OS
video spec already identifies keyframe sampling as the stage that dominates video
processing time; running a detector per frame would be far worse and buys nothing
for shot selection, where one representative frame per scene is the unit.

Gated behind `MediaProcessingConfig.enable_image_processing`, which stays
`default=False`, and not auto-registered in `media/pipeline_registry.py`.

## Testing

Parser, against `parse_visual_regions`:

- A well-formed payload yields labelled regions with correct boxes.
- An undeclared label raises, naming it.
- A box outside 0–1, or with non-positive extent, raises via the locator
  validator — not clamped.
- `confidence` outside `[0.0, 1.0]` raises; a detection below `min_confidence` is
  dropped without error.
- Empty `regions` yields no regions and does not raise.

Pipeline and expander:

- `process` returns exactly one representation whose payload holds the batch.
- The expander produces one representation per region, each with a single
  `ImageRegionLocator` and its label.
- Registration through `register_representation_batch` leaves every region
  current; a second generation retires the first whole.

Graph:

- A visual region projects to `HAS_VISUAL_REGION` with its label and box.
- **Regression guard:** an OCR representation projected alongside a visual region
  still reaches its own edge and does not become a `VisualRegion`. Verify this
  guard by temporarily dispatching on `locator_type` and confirming the test
  fails.

## Out of scope

Tracking a subject across frames, re-identification, face recognition or any
identity attribution, pose, depth, and automatic reframing. katsi reports where a
labelled thing appears in a sampled frame; deciding how to crop around it is an
edit-time decision and belongs to the consumer, consistent with the boundary set
in the audio precision design.

Person detection is permitted as a *label* like any other. Identifying *who* a
person is stays out, matching the deliberate anonymous-only restriction already
enforced on speaker segmentation (`media/audio_pipeline.py:1015`).

## Follow-on

Once regions exist, `list_media_representations` already enumerates them —
`kinds=["visual_region"]` needs no new tool. A consumer filters by label in the
graph, then reads boxes from the locators it gets back.
