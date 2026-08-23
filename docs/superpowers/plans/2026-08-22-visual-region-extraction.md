# Visual Region Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let katsi answer where in a video frame a labelled subject appears, not only when it occurs.

**Architecture:** An owner-supplied detector runs over keyframes the video pipeline already extracted. Each detection becomes its own representation carrying one `ImageRegionLocator` and its label, because the shared locator type has a box but no label. Registration goes through `register_representation_batch` so all N detections of one generation stay visible together.

**Tech Stack:** Python 3.12+, pydantic, Kùzu, pytest. The detector is invoked only through `BoundedSubprocessExecutor` under an owner-authored `MediaPipelineDefinition` — katsi imports no vision library and downloads no weights.

**Spec:** `docs/superpowers/specs/2026-08-22-visual-region-extraction-design.md`

## Global Constraints

- katsi reports *where a labelled thing appears*. No tracking, re-identification, face recognition, identity attribution, pose, depth, or automatic reframing. Cropping is an edit-time decision belonging to the consumer.
- Person detection is allowed as a label. Identifying *who* a person is stays out, matching the anonymous-only restriction on speaker segmentation (`media/audio_pipeline.py:1015`).
- Every subprocess runs through `BoundedSubprocessExecutor` with `network_disabled=True` and `strict_output_contract=True`. Never call `subprocess` directly.
- No new Python dependency. The detector is an owner-supplied executable.
- `MediaProcessingConfig.enable_image_processing` (`media/contracts.py`) stays `default=False`, and the pipeline is not added to `media/pipeline_registry.py`.
- Detection runs on keyframes only, never per frame.
- All paths are relative to `/Users/jeanhrdz/katsi`. Run tests with `.venv/bin/pytest`.

---

### Task 1: Parse the detector's output

**Files:**
- Modify: `packages/core/katsi_core/media/image_pipeline.py` (add after `_parse_ocr_regions`, which ends at `:341`)
- Test: `tests/test_media_image_pipeline.py` (new `class TestVisualRegionParsing`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_VisualRegion(label: str, bbox: tuple[float, float, float, float], confidence: float | None)` and `parse_visual_regions(payload: dict, *, allowed_labels: set[str], min_confidence: float = 0.3) -> list[_VisualRegion]`.

**Background the implementer needs — do not copy the neighbouring OCR parser's behaviour.** `_parse_ocr_regions` (`:319`) *silently skips* malformed entries, and its docstring explains why: "a single bad region should not discard an otherwise valid whole-image OCR result". OCR has a whole-image payload worth preserving. Detection does not — the regions **are** the result, so a malformed one is a contract violation and must raise, matching every other strict parser in this codebase. This difference is deliberate.

Do not pre-clamp boxes. `ImageRegionLocator` (`media/contracts.py:211`) validates that coordinates are normalised, extents positive, and `x + w <= 1`; a detector emitting out-of-range boxes is misconfigured, and silently fixing it hides that.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_image_pipeline.py`:

```python
class TestVisualRegionParsing:
    ALLOWED = {"train", "person", "food"}

    def test_parses_labelled_regions(self):
        payload = {
            "regions": [
                {"label": "train", "bounding_box": [0.1, 0.2, 0.4, 0.5], "confidence": 0.9},
                {"label": "person", "bounding_box": [0.6, 0.1, 0.2, 0.3]},
            ]
        }

        regions = parse_visual_regions(payload, allowed_labels=self.ALLOWED)

        assert [r.label for r in regions] == ["train", "person"]
        assert regions[0].bbox == (0.1, 0.2, 0.4, 0.5)
        assert regions[0].confidence == 0.9
        assert regions[1].confidence is None

    def test_empty_regions_is_valid(self):
        assert parse_visual_regions({"regions": []}, allowed_labels=self.ALLOWED) == []

    def test_missing_regions_key_raises(self):
        with pytest.raises(ValueError, match="regions"):
            parse_visual_regions({}, allowed_labels=self.ALLOWED)

    def test_undeclared_label_raises(self):
        payload = {"regions": [{"label": "spaceship", "bounding_box": [0.1, 0.1, 0.2, 0.2]}]}

        with pytest.raises(ValueError, match="spaceship"):
            parse_visual_regions(payload, allowed_labels=self.ALLOWED)

    def test_malformed_region_raises_rather_than_skipping(self):
        # Unlike _parse_ocr_regions, a bad entry is fatal: the regions are
        # the entire result, so silently dropping one loses the answer.
        payload = {"regions": [{"label": "train", "bounding_box": [0.1, 0.2]}]}

        with pytest.raises(ValueError, match="four numbers"):
            parse_visual_regions(payload, allowed_labels=self.ALLOWED)

    def test_non_object_region_raises(self):
        with pytest.raises(ValueError, match="JSON object"):
            parse_visual_regions({"regions": ["train"]}, allowed_labels=self.ALLOWED)

    def test_confidence_out_of_range_raises(self):
        payload = {
            "regions": [{"label": "train", "bounding_box": [0.1, 0.1, 0.2, 0.2], "confidence": 1.4}]
        }

        with pytest.raises(ValueError, match="confidence"):
            parse_visual_regions(payload, allowed_labels=self.ALLOWED)

    def test_low_confidence_detections_are_dropped(self):
        payload = {
            "regions": [
                {"label": "train", "bounding_box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9},
                {"label": "person", "bounding_box": [0.5, 0.5, 0.2, 0.2], "confidence": 0.05},
            ]
        }

        regions = parse_visual_regions(payload, allowed_labels=self.ALLOWED, min_confidence=0.3)

        assert [r.label for r in regions] == ["train"]

    def test_out_of_range_box_is_not_clamped(self):
        # Left to ImageRegionLocator's validator in the expander; the parser
        # must not quietly repair it here either.
        payload = {"regions": [{"label": "train", "bounding_box": [0.9, 0.1, 0.5, 0.2]}]}

        regions = parse_visual_regions(payload, allowed_labels=self.ALLOWED)

        assert regions[0].bbox == (0.9, 0.1, 0.5, 0.2)
```

Add `parse_visual_regions` to the file's `katsi_core.media.image_pipeline` import block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py::TestVisualRegionParsing -v`
Expected: FAIL — `ImportError: cannot import name 'parse_visual_regions'`

- [ ] **Step 3: Implement the parser**

Add to `packages/core/katsi_core/media/image_pipeline.py`, after `_parse_ocr_regions`:

```python
@dataclass(frozen=True, slots=True)
class _VisualRegion:
    """One labelled detection inside a sampled frame."""

    label: str
    bbox: tuple[float, float, float, float]
    confidence: float | None


def parse_visual_regions(
    payload: dict[str, Any],
    *,
    allowed_labels: set[str],
    min_confidence: float = 0.3,
) -> list[_VisualRegion]:
    """Strictly parse a detector's `regions` array.

    Unlike :func:`_parse_ocr_regions`, a malformed entry raises rather than
    being skipped: OCR has a whole-image result worth preserving, whereas
    here the regions are the entire result. Boxes are never clamped --
    :class:`ImageRegionLocator` validates them, and a detector emitting
    out-of-range boxes is misconfigured.
    """
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list):
        raise ValueError("Detector output must carry a `regions` array")

    regions: list[_VisualRegion] = []
    for entry in raw_regions:
        if not isinstance(entry, dict):
            raise ValueError("Each region must be a JSON object")

        label = entry.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError("Region label must be a non-empty string")
        if label not in allowed_labels:
            raise ValueError(f"Region label is not in the declared label set: {label!r}")

        bbox = entry.get("bounding_box")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"Region bounding_box must be four numbers, got {bbox!r}")
        try:
            bbox_tuple = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Region bounding_box must be four numbers, got {bbox!r}") from exc

        confidence = entry.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not (0.0 <= confidence <= 1.0):
                raise ValueError("Region confidence must be within [0.0, 1.0]")
            if confidence < min_confidence:
                continue

        regions.append(_VisualRegion(label=label, bbox=bbox_tuple, confidence=confidence))

    return regions
```

Confirm `dataclass` and `Any` are already imported in this module; add them if not.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py::TestVisualRegionParsing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/core/katsi_core/media/image_pipeline.py tests/test_media_image_pipeline.py
git commit -m "feat: parse labelled visual regions from a detector

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Representation kind, stage, and the expander

**Files:**
- Modify: `packages/core/katsi_core/media/contracts.py` (add to `MediaRepresentationKind` after `SILENCE_SPAN`; add to `PipelineStage` after `DETECT_SILENCE`)
- Modify: `tests/test_media_contracts.py:61-74` (the exact-set assertion)
- Modify: `packages/core/katsi_core/media/image_pipeline.py` (add the expander after `parse_visual_regions`)
- Test: `tests/test_media_image_pipeline.py` (new `class TestVisualRegionRepresentations`)

**Interfaces:**
- Consumes: `_VisualRegion` and `parse_visual_regions` from Task 1.
- Produces: `MediaRepresentationKind.VISUAL_REGION`, `PipelineStage.DETECT_REGIONS`, and `build_visual_region_representations(regions: list[_VisualRegion], resource_version_id: ResourceVersionId, pipeline_fingerprint: PipelineFingerprint, adapter: ProducerProvenance) -> list[DerivedRepresentation]`.

**Background the implementer needs:** `tests/test_media_contracts.py::test_media_representation_kind_enum_is_complete` asserts the **exact** set of kind values. Adding a member breaks it, and updating that set is part of this task, not a regression.

`MediaCoverage` rejects `is_complete=True` unless `coverage_fraction=1.0`. A single region covers part of a frame, so use `is_complete=False`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_image_pipeline.py`:

```python
class TestVisualRegionRepresentations:
    def _provenance(self):
        return ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="image_detect_regions",
            adapter_version="1.0.0",
        )

    def _fingerprint(self):
        return PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.VISUAL_REGION,
            stage=PipelineStage.DETECT_REGIONS,
            adapter_name="image_detect_regions",
            adapter_version="1.0.0",
            sampling_fingerprint="b" * 64,
        )

    def test_one_representation_per_region(self):
        resource_version_id = uuid4()
        regions = [
            _VisualRegion(label="train", bbox=(0.1, 0.2, 0.4, 0.5), confidence=0.9),
            _VisualRegion(label="person", bbox=(0.6, 0.1, 0.2, 0.3), confidence=None),
        ]

        representations = build_visual_region_representations(
            regions, resource_version_id, self._fingerprint(), self._provenance()
        )

        assert len(representations) == 2
        first = representations[0]
        assert first.kind == MediaRepresentationKind.VISUAL_REGION
        assert first.textual_payload == "train"
        assert first.confidence == 0.9
        assert len(first.locators) == 1
        assert first.locators[0].locator_type == "image_region"
        assert first.locators[0].bounding_box == (0.1, 0.2, 0.4, 0.5)
        assert first.coverage.is_complete is False

    def test_no_regions_produces_no_representations(self):
        assert (
            build_visual_region_representations(
                [], uuid4(), self._fingerprint(), self._provenance()
            )
            == []
        )

    def test_out_of_bounds_box_is_rejected_by_the_locator(self):
        # x + w exceeds 1: ImageRegionLocator must refuse it rather than
        # anything upstream silently clamping.
        regions = [_VisualRegion(label="train", bbox=(0.9, 0.1, 0.5, 0.2), confidence=None)]

        with pytest.raises(ValueError, match="normalized"):
            build_visual_region_representations(
                regions, uuid4(), self._fingerprint(), self._provenance()
            )
```

Add `_VisualRegion` and `build_visual_region_representations` to the import block, and `PipelineFingerprint`, `PipelineStage`, `ProducerProvenance`, `MediaProducerType` from `katsi_core.media.contracts` if not already imported there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py::TestVisualRegionRepresentations -v`
Expected: FAIL — `ImportError: cannot import name 'build_visual_region_representations'`

- [ ] **Step 3: Add the enum members**

In `packages/core/katsi_core/media/contracts.py`, add to `MediaRepresentationKind` immediately after `SILENCE_SPAN = "silence_span"`:

```python
    VISUAL_REGION = "visual_region"
```

And to `PipelineStage` immediately after `DETECT_SILENCE = "detect_silence"`:

```python
    DETECT_REGIONS = "detect_regions"
```

- [ ] **Step 4: Update the exact-set assertion**

In `tests/test_media_contracts.py`, inside `test_media_representation_kind_enum_is_complete`'s `expected_kinds`, add after `"silence_span",`:

```python
        "visual_region",
```

- [ ] **Step 5: Implement the expander**

Add to `packages/core/katsi_core/media/image_pipeline.py`, after `parse_visual_regions`:

```python
def build_visual_region_representations(
    regions: list[_VisualRegion],
    resource_version_id: ResourceVersionId,
    pipeline_fingerprint: PipelineFingerprint,
    adapter: ProducerProvenance,
) -> list[DerivedRepresentation]:
    """Expand detections into one addressable representation each.

    One representation per detection rather than one carrying many boxes:
    :class:`ImageRegionLocator` has a bounding box but no label, and a
    consumer must be able to cite a single detection as evidence.

    An empty list yields no representations. A frame containing nothing of
    interest is a real answer, not a failure.
    """
    now = datetime.now(UTC)
    representations: list[DerivedRepresentation] = []

    for region in regions:
        rep_id = uuid4()
        representations.append(
            DerivedRepresentation(
                id=rep_id,
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.VISUAL_REGION,
                media_type="application/json",
                status=MediaRepresentationStatus.CURRENT,
                created_at=now,
                updated_at=now,
                textual_payload=region.label,
                locators=(
                    ImageRegionLocator(
                        resource_version_id=resource_version_id,
                        representation_id=rep_id,
                        bounding_box=region.bbox,
                    ),
                ),
                coverage=MediaCoverage(
                    is_complete=False,
                    coverage_fraction=min(1.0, region.bbox[2] * region.bbox[3]),
                    detail=f"detected region: {region.label}",
                ),
                confidence=region.confidence,
                producer=adapter,
                pipeline_fingerprint=pipeline_fingerprint,
            )
        )

    return representations
```

`coverage_fraction` is the box's area as a fraction of the frame, which is the honest reading of "how much of the source this representation covers".

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py::TestVisualRegionRepresentations tests/test_media_contracts.py -v`
Expected: PASS

- [ ] **Step 7: Run the full media suite**

Run: `.venv/bin/pytest tests/ -k media -q`
Expected: PASS. New enum members are additive.

- [ ] **Step 8: Commit**

```bash
git add packages/core/katsi_core/media/contracts.py packages/core/katsi_core/media/image_pipeline.py tests/test_media_contracts.py tests/test_media_image_pipeline.py
git commit -m "feat: expand detections into addressable visual region representations

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The detection pipeline

**Files:**
- Modify: `packages/core/katsi_core/media/image_pipeline.py` (add after the expander)
- Test: `tests/test_media_image_pipeline.py` (new `class TestVisualRegionDetectionPipeline`)

**Interfaces:**
- Consumes: `parse_visual_regions` from Task 1; `MediaRepresentationKind.VISUAL_REGION` and `PipelineStage.DETECT_REGIONS` from Task 2.
- Produces: `build_region_detect_definition(*, executable_path="detect-regions", labels: tuple[str, ...], min_confidence=0.3, timeout_seconds=120.0, max_output_bytes=1_000_000) -> MediaPipelineDefinition` and `VisualRegionDetectionPipeline`, whose `process` returns a single `DerivedRepresentation`.

**Background the implementer needs:** `PipelineExecutionOrchestrator.run` calls `adapter.process(file_path, resource_version_id, source_content_hash, pipeline_fingerprint, working_directory)` — exactly five positional arguments. `process` must not require anything else and must return **one** `DerivedRepresentation`, not a list, because the representation cache keys one per fingerprint. The expander from Task 2 fans it out separately. This mirrors `ImageOcrPipeline` and `AudioTranscriptionPipeline`.

The detector writes JSON to `{output_path}`, like the OCR and caption adapters, rather than to stdout.

Tests fake the executable with `sys.executable` and an inline `-c` script, the pattern already used in this file (see `tests/test_media_image_pipeline.py:441`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_image_pipeline.py`:

```python
class TestVisualRegionDetectionPipeline:
    LABELS = ("train", "person")

    def _fake_definition(self, payload_json: str, *, exit_code: int = 0):
        script = (
            "import sys, pathlib\n"
            f"pathlib.Path(sys.argv[2]).write_text({payload_json!r})\n"
            f"sys.exit({exit_code})\n"
        )
        return build_region_detect_definition(labels=self.LABELS).model_copy(
            update={
                "executable_path": sys.executable,
                "fixed_args": ["-c", script, "{input_path}", "{output_path}"],
            }
        )

    def _fingerprint(self, source_content_hash):
        return build_pipeline_fingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.VISUAL_REGION,
            stage=PipelineStage.DETECT_REGIONS,
            adapter_name="image_detect_regions",
            adapter_version="1.0.0",
            settings=MediaSamplingSettings(),
        )

    def test_process_returns_single_batch_representation(self, tmp_path):
        resource_version_id, source_content_hash = uuid4(), "a" * 64
        input_path = tmp_path / "keyframe.png"
        input_path.write_bytes(b"not really a png")
        payload = json.dumps(
            {"regions": [{"label": "train", "bounding_box": [0.1, 0.2, 0.4, 0.5], "confidence": 0.9}]}
        )

        adapter = VisualRegionDetectionPipeline(self._fake_definition(payload))
        representation = adapter.process(
            input_path,
            resource_version_id,
            source_content_hash,
            self._fingerprint(source_content_hash),
            tmp_path,
        )

        assert isinstance(representation, DerivedRepresentation)
        assert representation.kind == MediaRepresentationKind.VISUAL_REGION
        batch = json.loads(representation.textual_payload)
        assert batch["regions"] == [
            {"label": "train", "bounding_box": [0.1, 0.2, 0.4, 0.5], "confidence": 0.9}
        ]

    def test_undeclared_label_from_detector_raises(self, tmp_path):
        resource_version_id, source_content_hash = uuid4(), "a" * 64
        input_path = tmp_path / "keyframe.png"
        input_path.write_bytes(b"x")
        payload = json.dumps(
            {"regions": [{"label": "spaceship", "bounding_box": [0.1, 0.1, 0.2, 0.2]}]}
        )

        adapter = VisualRegionDetectionPipeline(self._fake_definition(payload))

        with pytest.raises(ValueError, match="spaceship"):
            adapter.process(
                input_path,
                resource_version_id,
                source_content_hash,
                self._fingerprint(source_content_hash),
                tmp_path,
            )

    def test_nonzero_exit_raises(self, tmp_path):
        resource_version_id, source_content_hash = uuid4(), "a" * 64
        input_path = tmp_path / "keyframe.png"
        input_path.write_bytes(b"x")

        adapter = VisualRegionDetectionPipeline(
            self._fake_definition(json.dumps({"regions": []}), exit_code=1)
        )

        with pytest.raises(RuntimeError, match="Region detection failed"):
            adapter.process(
                input_path,
                resource_version_id,
                source_content_hash,
                self._fingerprint(source_content_hash),
                tmp_path,
            )

    def test_empty_detection_is_not_a_failure(self, tmp_path):
        resource_version_id, source_content_hash = uuid4(), "a" * 64
        input_path = tmp_path / "keyframe.png"
        input_path.write_bytes(b"x")

        adapter = VisualRegionDetectionPipeline(
            self._fake_definition(json.dumps({"regions": []}))
        )
        representation = adapter.process(
            input_path,
            resource_version_id,
            source_content_hash,
            self._fingerprint(source_content_hash),
            tmp_path,
        )

        assert json.loads(representation.textual_payload)["regions"] == []

    def test_definition_declares_labels_and_is_offline(self):
        definition = build_region_detect_definition(labels=self.LABELS)

        assert definition.producer_type == MediaProducerType.MODEL_BACKED
        assert definition.network_disabled is True
        assert definition.stage == PipelineStage.DETECT_REGIONS
        assert definition.input_kinds == [MediaRepresentationKind.KEYFRAME]
        assert "train,person" in " ".join(definition.fixed_args)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py::TestVisualRegionDetectionPipeline -v`
Expected: FAIL — `ImportError: cannot import name 'build_region_detect_definition'`

- [ ] **Step 3: Implement the definition builder**

```python
def build_region_detect_definition(
    *,
    executable_path: str = "detect-regions",
    labels: tuple[str, ...],
    min_confidence: float = 0.3,
    timeout_seconds: float = 120.0,
    max_output_bytes: int = 1_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for local open-vocabulary region detection.

    ``labels`` is the owner's declared label set: it is passed to the
    executable and is what parsing validates against, so katsi enforces the
    contract without taking a position on what the labels mean. The
    configured executable wraps a local detector and writes JSON to
    ``output_path`` with a required ``regions`` array.
    """
    if not labels:
        raise ValueError("A detector definition must declare at least one label")
    return MediaPipelineDefinition(
        id="image_detect_regions_v1",
        name="Local visual region detection",
        description="Optional local open-vocabulary detection over sampled keyframes.",
        stage=PipelineStage.DETECT_REGIONS,
        accepted_mime_patterns=["image/*"],
        input_kinds=[MediaRepresentationKind.KEYFRAME],
        representation_kinds_produced=[MediaRepresentationKind.VISUAL_REGION],
        producer_type=MediaProducerType.MODEL_BACKED,
        executable_path=executable_path,
        fixed_args=[
            "{input_path}",
            "{output_path}",
            "--labels",
            ",".join(labels),
            "--min-confidence",
            str(min_confidence),
        ],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )
```

- [ ] **Step 4: Implement the pipeline**

```python
class VisualRegionDetectionPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing labelled regions for one keyframe.

    Consumes keyframes the video pipeline already extracted, so it never
    decodes video and never runs per frame. ``process`` returns one
    representation carrying the validated batch; use
    :func:`build_visual_region_representations` to expand it into the N
    per-detection representations.
    """

    def __init__(self, definition: MediaPipelineDefinition | None = None) -> None:
        if definition is None:
            raise ValueError("A detector definition with a declared label set is required")
        self._definition = definition
        self._executor = BoundedSubprocessExecutor()
        self._allowed_labels = _labels_from_definition(definition)
        self._min_confidence = _min_confidence_from_definition(definition)

    @classmethod
    def get_adapter_name(cls) -> str:
        return "image_detect_regions"

    @classmethod
    def get_adapter_version(cls) -> str:
        return "1.0.0"

    def get_pipeline_definition(self) -> MediaPipelineDefinition:  # type: ignore[override]
        return self._definition

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        output_path = working_directory / "regions.json"
        result = self._executor.execute(
            self._definition, file_path, working_directory, output_path=output_path
        )

        if result.timed_out or result.exit_code != 0 or not output_path.exists():
            raise RuntimeError(
                f"Region detection failed: exit_code={result.exit_code} "
                f"timed_out={result.timed_out} stderr={result.stderr_sample[:500]!r}"
            )

        try:
            payload = json.loads(output_path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Detector output is not valid JSON: {exc}") from exc

        # Validates labels and boxes; raises rather than repairing.
        parse_visual_regions(
            payload,
            allowed_labels=self._allowed_labels,
            min_confidence=self._min_confidence,
        )

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.VISUAL_REGION,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(payload, sort_keys=True),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(
                is_complete=True,
                coverage_fraction=1.0,
                detail="regions detected across the whole frame",
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
                adapter_name=self.get_adapter_name(),
                adapter_version=self.get_adapter_version(),
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: Any, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Expected a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.VISUAL_REGION:
            return False, "Expected a VISUAL_REGION representation"
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "Expected CURRENT status for successful detection"
        return True, None
```

Add the two small helpers immediately above the class, so the label set and
threshold are read back from the definition rather than duplicated:

```python
def _labels_from_definition(definition: MediaPipelineDefinition) -> set[str]:
    args = list(definition.fixed_args)
    if "--labels" not in args:
        raise ValueError("Detector definition must declare --labels")
    return {label for label in args[args.index("--labels") + 1].split(",") if label}


def _min_confidence_from_definition(definition: MediaPipelineDefinition) -> float:
    args = list(definition.fixed_args)
    if "--min-confidence" not in args:
        return 0.3
    return float(args[args.index("--min-confidence") + 1])
```

Note the coverage on the batch representation is complete: it accounts for the whole frame's analysis. Per-region coverage is the box area, set by the Task 2 expander.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_image_pipeline.py -v`
Expected: PASS

- [ ] **Step 6: Confirm the feature stays gated**

Run: `grep -n "enable_image_processing" packages/core/katsi_core/media/contracts.py`
Expected: still `default=False`.

Run: `grep -rn "VisualRegionDetectionPipeline" packages/core/katsi_core/media/pipeline_registry.py`
Expected: no matches.

- [ ] **Step 7: Commit**

```bash
git add packages/core/katsi_core/media/image_pipeline.py tests/test_media_image_pipeline.py
git commit -m "feat: add visual region detection pipeline

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Project visual regions into the graph

**Files:**
- Modify: `packages/core/katsi_core/store/graph.py` (node table near `:79`, rel table near `:100`, branch in `_project_media_locator`)
- Test: `tests/test_media_vector_projection.py`

**Interfaces:**
- Consumes: `MediaRepresentationKind.VISUAL_REGION` from Task 2.
- Produces: a `VisualRegion(id, label, x, y, width, height)` node table and a `HAS_VISUAL_REGION` relation from `MediaResourceVersion`.

**Background the implementer needs — this trap has already bitten this codebase twice.** `_project_media_locator` dispatches on `locator_type` first, then falls through to `elif item.kind is ...` branches. **OCR representations also carry `image_region` locators** (`media/image_pipeline.py:418`). So adding `elif locator_type == "image_region"` would capture OCR output and route it into `HAS_VISUAL_REGION`.

Dispatch on `item.kind is MediaRepresentationKind.VISUAL_REGION`, placed beside the existing `SILENCE_SPAN` and `TRANSCRIPT_SEGMENT` kind branches. The same guard exists for `time_range`; read those branches before writing yours.

The label is stored on the node so a consumer can filter by label in Kùzu without a second lookup, following the `MediaPage.number` precedent (`:67`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_media_vector_projection.py`, reusing the file's existing helpers:

```python
def _visual_region_representation(*, label: str = "train", bbox=(0.1, 0.2, 0.4, 0.5)):
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.VISUAL_REGION,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=label,
        locators=(
            ImageRegionLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                bounding_box=bbox,
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.2),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="image_detect_regions",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.VISUAL_REGION,
            stage=PipelineStage.DETECT_REGIONS,
            adapter_name="image_detect_regions",
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def _ocr_representation():
    """OCR also carries image_region locators -- the trap this guards."""
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="PLATFORM 3",
        locators=(
            ImageRegionLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                bounding_box=(0.5, 0.5, 0.2, 0.1),
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.1),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="image_ocr",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="c" * 64,
            representation_kind=MediaRepresentationKind.OCR_TEXT,
            stage=PipelineStage.OCR,
            adapter_name="image_ocr",
            adapter_version="1",
            sampling_fingerprint="d" * 64,
        ),
    )


def test_visual_region_projects_with_label_and_box(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _visual_region_representation(label="train", bbox=(0.1, 0.2, 0.4, 0.5))

    graph.project_media_representations([representation])

    row = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_VISUAL_REGION]->(v:VisualRegion) "
        "RETURN v.label, v.x, v.y, v.width, v.height",
        {"id": str(representation.resource_version_id)},
    ).get_next()
    assert row[0] == "train"
    assert row[1:] == pytest.approx([0.1, 0.2, 0.4, 0.5])


def test_ocr_is_not_captured_as_a_visual_region(tmp_path):
    """Regression guard: OCR also carries image_region locators."""
    graph = GraphStore(tmp_path / "graph")
    ocr = _ocr_representation()

    graph.project_media_representations([ocr, _visual_region_representation()])

    count = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_VISUAL_REGION]->(v:VisualRegion) "
        "RETURN count(v)",
        {"id": str(ocr.resource_version_id)},
    ).get_next()[0]
    assert count == 0
```

Add `ImageRegionLocator` to this file's `katsi_core.media.contracts` import block.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_media_vector_projection.py -k visual -v`
Expected: FAIL — the `VisualRegion` table does not exist.

- [ ] **Step 3: Add the schema**

In `packages/core/katsi_core/store/graph.py`, after the `SilenceSpan` node table:

```python
        # Label stored on the node so a consumer can filter by label in Kùzu
        # without a second lookup, following the MediaPage.number precedent.
        self._conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS VisualRegion("
            "id STRING, label STRING, x DOUBLE, y DOUBLE, width DOUBLE, height DOUBLE, "
            "PRIMARY KEY(id))"
        )
```

After the `HAS_SILENCE_SPAN` rel table:

```python
        self._conn.execute(
            "CREATE REL TABLE IF NOT EXISTS HAS_VISUAL_REGION(FROM MediaResourceVersion TO VisualRegion)"
        )
```

- [ ] **Step 4: Add the projection branch**

In `_project_media_locator`, immediately before the existing
`elif item.kind is MediaRepresentationKind.SILENCE_SPAN:` branch:

```python
            # Dispatched on kind, not locator_type: OCR representations also
            # carry image_region locators and must keep their own edge.
            elif item.kind is MediaRepresentationKind.VISUAL_REGION:
                x, y, width, height = locator_data["bounding_box"]
                self._conn.execute(
                    "MERGE (v:VisualRegion {id: $id}) "
                    "SET v.label = $label, v.x = $x, v.y = $y, "
                    "v.width = $width, v.height = $height",
                    {
                        "id": str(item.id),
                        "label": item.textual_payload or "",
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                    },
                )
                self._connect_media_node(
                    resource_id, "VisualRegion", str(item.id), "HAS_VISUAL_REGION"
                )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_media_vector_projection.py -v`
Expected: PASS

- [ ] **Step 6: Prove the regression guard actually catches the trap**

Temporarily change the branch condition to `elif locator_type == "image_region":`, then run:

`.venv/bin/pytest tests/test_media_vector_projection.py::test_ocr_is_not_captured_as_a_visual_region -v`

Expected: FAIL. Then revert the condition and confirm the test passes again. A guard that has never been seen to fail is not a guard.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/core/katsi_core/store/graph.py tests/test_media_vector_projection.py
git commit -m "feat: project visual regions into the media graph

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Verification

- [ ] `.venv/bin/pytest tests/ -q` passes.
- [ ] `grep -rinE "\btrack(ing)?\b|reidentif|face_recognition|\bpose\b|\bdepth\b" packages/core/katsi_core/media/image_pipeline.py` returns no matches — the out-of-scope boundary held.
- [ ] `grep -n "enable_image_processing" packages/core/katsi_core/media/contracts.py` still shows `default=False`.
- [ ] `grep -rn "VisualRegionDetectionPipeline" packages/core/katsi_core/media/pipeline_registry.py` returns nothing.
- [ ] `list_media_representations(workspace_id, path, kinds=["visual_region"])` needs no change to work — confirm by reading the tool; it validates kinds against `MediaRepresentationKind`, which now includes the new member.
