"""Image and screenshot understanding: OCR, captioning, embedding, thumbnails.

Implements openspec change `multimedia-understanding` section 5 (Image and
Screenshot Understanding, design.md Decision 6):

1. Deterministic metadata (dimensions, orientation, color/alpha, classified
   fields, privacy-gated EXIF location) lives in `image_metadata.py`.
2. Orientation-normalized thumbnails, whole-image/region-aware OCR, optional
   local captioning, and optional visual embedding are each a
   `MediaPipelineDefinition` + `MediaPipelineProtocol` adapter, executed
   exclusively through `BoundedSubprocessExecutor`/`PipelineExecutionOrchestrator`
   (task 5.2-5.5). This module never imports or calls an OCR/vision/embedding
   library directly -- it only defines the fixed subprocess contract (an
   owner-configured executable that writes a small JSON or PNG file the
   adapter here strictly validates) and wraps the result into a
   `DerivedRepresentation`.
3. Each representation kind (THUMBNAIL, OCR_TEXT, IMAGE_CAPTION,
   VISUAL_EMBEDDING) is produced by its own independent pipeline with its
   own `input_kinds=[]` (all consume the raw source image directly, not each
   other's output), so any valid subset remains usable if a sibling fails or
   is unavailable (task 5.6) -- mirroring the "detect -> independent
   branches" DAG shape in design.md Decision 5.

Reconciliation note for section 6: `ImageOcrPipeline` is the adapter
`DocumentOcrCoordinator` (in `document_pipeline.py`) expects to find via
`MediaPipelineRegistry.resolve("image/png", MediaRepresentationKind.OCR_TEXT)`.
It supports zero-argument construction (`ImageOcrPipeline()`) so it works
with that coordinator's `registered.adapter_class()` call pattern; its
`get_pipeline_definition()` classmethod is the single source of truth for
its own executable/contract, consistent with what should be passed to
`MediaPipelineRegistry.register(...)`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from katsi_core.media.blob_store import BlobStore
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    ImageRegionLocator,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.detection import _png_dimensions
from katsi_core.media.execution import BoundedSubprocessExecutor, validate_json_output
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

_ADAPTER_VERSION = "1.0.0"

_CAPTION_MAX_CHARS = 2000
_MAX_EMBEDDING_DIMENSION = 8192


# =============================================================================
# Task 5.2: orientation-normalized thumbnails
# =============================================================================


def build_thumbnail_pipeline_definition(
    executable_path: str | None = None,
    *,
    id: str = "image_thumbnail_v1",  # noqa: A002 -- matches MediaPipelineDefinition.id
    fixed_args: list[str] | None = None,
    max_dimension: int = 512,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 20_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for the bounded thumbnail pipeline.

    `executable_path` is owner-supplied (a tool that reads `input_path`,
    applies EXIF orientation normalization, downsizes to at most
    `max_dimension` on the long edge, and writes a PNG to `output_path`).
    Unset by default so `check_availability` reports unavailable rather
    than guessing at a system tool.
    """
    return MediaPipelineDefinition(
        id=id,
        name="Orientation-Normalized Thumbnail",
        description=(
            "Renders a private, orientation-normalized PNG thumbnail without "
            "altering the original image bytes."
        ),
        stage=PipelineStage.GENERATE_THUMBNAIL,
        accepted_mime_patterns=["image/*"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.THUMBNAIL],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=fixed_args or ["{input_path}", str(max_dimension), "{output_path}"],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


def build_sips_heic_thumbnail_pipeline_definition(
    executable_path: str | None = None,
    *,
    max_dimension: int = 512,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 20_000_000,
) -> MediaPipelineDefinition:
    """Build the owner-configured macOS ``sips`` HEIC thumbnail adapter."""
    definition = build_thumbnail_pipeline_definition(
        executable_path,
        id="sips_heic_thumbnail_v1",
        fixed_args=[
            "-s",
            "format",
            "png",
            "-Z",
            str(max_dimension),
            "{input_path}",
            "--out",
            "{output_path}",
        ],
        max_dimension=max_dimension,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    return definition.model_copy(
        update={
            "name": "HEIC PNG Thumbnail (macOS sips)",
            "accepted_mime_patterns": ["image/heic"],
        }
    )


class ImageThumbnailPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing a private PNG thumbnail.

    Original bytes are never written to; the thumbnail is a fresh derived
    blob in the blob store, never a replacement for the source file. Only
    `BoundedSubprocessExecutor` invokes the resize/rotate tool.
    """

    def __init__(
        self,
        definition: MediaPipelineDefinition | None = None,
        blob_store: BlobStore | None = None,
    ) -> None:
        self._definition = definition or self.get_pipeline_definition()
        self._blob_store = blob_store
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "image_thumbnail_pipeline"

    @classmethod
    def get_adapter_version(cls) -> str:
        return _ADAPTER_VERSION

    def get_pipeline_definition(self) -> MediaPipelineDefinition:  # type: ignore[override]
        return build_thumbnail_pipeline_definition()

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def check_availability(self) -> tuple[bool, str | None]:  # type: ignore[override]
        if not self._definition.executable_path:
            return False, "No thumbnail renderer executable configured"
        return True, None

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        if self._blob_store is None:
            raise RuntimeError("ImageThumbnailPipeline requires a blob_store to persist output")

        output_path = working_directory / "thumbnail.png"
        result = self._executor.execute(
            self._definition, file_path, working_directory, output_path=output_path
        )
        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Thumbnail generation failed (exit={result.exit_code}, "
                f"timed_out={result.timed_out}): {result.stderr_sample}"
            )
        if not output_path.exists():
            raise RuntimeError("Thumbnail tool produced no output file")

        thumbnail_bytes = output_path.read_bytes()
        if not thumbnail_bytes:
            raise RuntimeError("Thumbnail tool produced an empty output file")

        dims = _png_dimensions(thumbnail_bytes[:64])
        if dims is None:
            raise RuntimeError("Thumbnail output is not a valid PNG")
        width, height = dims

        blob_hash, byte_count = self._blob_store.store_blob(thumbnail_bytes)
        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.THUMBNAIL,
            media_type="image/png",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            blob_reference=f"blob:{blob_hash}",
            blob_hash=blob_hash,
            blob_byte_count=byte_count,
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(
                is_complete=True,
                coverage_fraction=1.0,
                detail=f"{width}x{height} orientation-normalized thumbnail",
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name=self._definition.id,
                adapter_version=_ADAPTER_VERSION,
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: object, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Output is not a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.THUMBNAIL:
            return False, f"Expected THUMBNAIL, got {output.kind}"
        if output.status == MediaRepresentationStatus.CURRENT and (
            not output.blob_reference or not output.blob_hash or not output.blob_byte_count
        ):
            return False, "CURRENT thumbnail representation missing blob data"
        return True, None


# =============================================================================
# Task 5.3: whole-image and region-aware local OCR
# =============================================================================


def build_ocr_pipeline_definition(
    executable_path: str | None = None,
    *,
    id: str = "image_ocr_v1",  # noqa: A002
    fixed_args: list[str] | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 10_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for the bounded local OCR pipeline.

    The configured executable reads `input_path` and writes a JSON document
    to `output_path` with a required `text` key (whole-image OCR text) and
    an optional `regions` key (list of `{"text", "bbox", "confidence"}`,
    `bbox` normalized `[x, y, w, h]`) for region-aware evidence.
    """
    return MediaPipelineDefinition(
        id=id,
        name="Local Image OCR",
        description="Whole-image and region-aware local OCR with bounding-box locators.",
        stage=PipelineStage.OCR,
        accepted_mime_patterns=["image/*"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=fixed_args or ["{input_path}", "{output_path}"],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


@dataclass(frozen=True)
class _OcrRegion:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None


def _parse_ocr_regions(raw_regions: list[Any]) -> list[_OcrRegion]:
    """Parse and validate a `regions` array from OCR JSON output.

    Silently skips malformed individual region entries (a single bad region
    should not discard an otherwise valid whole-image OCR result) but never
    raises for well-formed-but-empty input.
    """
    regions: list[_OcrRegion] = []
    for entry in raw_regions:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        bbox = entry.get("bbox")
        confidence = entry.get("confidence")
        if not isinstance(text, str) or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            bbox_tuple = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            continue
        conf_value = float(confidence) if isinstance(confidence, (int, float)) else None
        regions.append(_OcrRegion(text=text, bbox=bbox_tuple, confidence=conf_value))
    return regions


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
    """Strictly parse a detector's ``regions`` array.

    Unlike :func:`_parse_ocr_regions`, a malformed entry raises rather than
    being skipped: OCR has a whole-image result worth preserving, whereas
    here the regions *are* the entire result, so dropping one loses the
    answer. Boxes are never clamped -- :class:`ImageRegionLocator` validates
    them, and a detector emitting out-of-range boxes is misconfigured.
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
            # Filtering by a declared threshold, not repairing bad output.
            if confidence < min_confidence:
                continue

        regions.append(_VisualRegion(label=label, bbox=bbox_tuple, confidence=confidence))

    return regions


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
                # Coverage is the box's area: how much of the frame this
                # representation actually accounts for.
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
    contract without taking a position on which detector is used or on what
    the labels mean. The configured executable wraps a local detector and
    writes JSON to ``output_path`` with a required ``regions`` array.
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


class VisualRegionDetectionPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing labelled regions for one keyframe.

    Consumes keyframes the video pipeline already extracted, so it never
    decodes video and never runs per frame. ``process`` returns one
    representation carrying the validated batch; use
    :func:`build_visual_region_representations` to expand it into the N
    per-detection representations.
    """

    def __init__(
        self,
        definition: MediaPipelineDefinition,
        *,
        labels: tuple[str, ...],
        min_confidence: float = 0.3,
    ) -> None:
        # Labels are passed explicitly rather than scraped back out of
        # `fixed_args`: an owner may legitimately customise the argument
        # template, and silently losing the label set would turn the
        # "closed" set open without anything failing.
        if not labels:
            raise ValueError("A detector pipeline must declare at least one label")
        self._definition = definition
        self._executor = BoundedSubprocessExecutor()
        self._allowed_labels = set(labels)
        self._min_confidence = min_confidence

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


class ImageOcrPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing whole-image and region OCR text.

    Supports zero-argument construction so it can be resolved and
    instantiated generically via `MediaPipelineRegistry` (see module
    docstring); `get_pipeline_definition()` is the single source of truth
    for its executable/contract rather than per-instance state.
    """

    def __init__(self) -> None:
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "image_ocr_pipeline"

    @classmethod
    def get_adapter_version(cls) -> str:
        return _ADAPTER_VERSION

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return build_ocr_pipeline_definition()

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.TESSERACT]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        definition = type(self).get_pipeline_definition()
        output_path = working_directory / "ocr.json"
        result = self._executor.execute(
            definition, file_path, working_directory, output_path=output_path
        )
        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"OCR failed (exit={result.exit_code}, timed_out={result.timed_out}): "
                f"{result.stderr_sample}"
            )
        if not output_path.exists():
            raise RuntimeError("OCR tool produced no output file")

        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"OCR output is not valid JSON: {e}") from e

        is_valid, error = validate_json_output(payload, {"text"}, {"text": str})
        if not is_valid:
            raise RuntimeError(f"OCR output failed contract validation: {error}")

        text = payload["text"]
        raw_regions = payload.get("regions", [])
        regions = _parse_ocr_regions(raw_regions if isinstance(raw_regions, list) else [])

        now = datetime.now(UTC)
        rep_id = uuid4()
        locators: list[Any] = [
            WholeResourceLocator(resource_version_id=resource_version_id, representation_id=rep_id)
        ]
        region_confidences: list[float] = []
        for region in regions:
            locators.append(
                ImageRegionLocator(
                    resource_version_id=resource_version_id,
                    representation_id=rep_id,
                    bounding_box=region.bbox,
                )
            )
            if region.confidence is not None:
                region_confidences.append(region.confidence)

        overall_confidence = payload.get("confidence")
        if isinstance(overall_confidence, (int, float)):
            confidence = float(overall_confidence)
        elif region_confidences:
            confidence = sum(region_confidences) / len(region_confidences)
        else:
            confidence = None

        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=text,
            locators=tuple(locators),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            confidence=confidence,
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name=definition.id,
                adapter_version=_ADAPTER_VERSION,
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: object, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Output is not a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.OCR_TEXT:
            return False, f"Expected OCR_TEXT, got {output.kind}"
        if output.status == MediaRepresentationStatus.CURRENT and output.textual_payload is None:
            return False, "CURRENT OCR representation missing textual_payload"
        if output.confidence is not None and not (0.0 <= output.confidence <= 1.0):
            return False, "OCR confidence out of [0, 1] range"
        return True, None


# =============================================================================
# Task 5.4: optional local image captioning
# =============================================================================


def build_caption_pipeline_definition(
    executable_path: str | None = None,
    *,
    id: str = "image_caption_v1",  # noqa: A002
    model_identity: str | None = None,
    model_version: str | None = None,
    fixed_args: list[str] | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 1_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for the optional local captioning pipeline.

    The configured executable wraps a local vision model and writes a JSON
    document to `output_path` with a required `caption` key (a single plain
    string, the strict caption contract) and optional `confidence`.
    """
    return MediaPipelineDefinition(
        id=id,
        name="Local Image Caption",
        description="Optional local image captioning through a configured vision adapter.",
        stage=PipelineStage.CAPTION,
        accepted_mime_patterns=["image/*"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.IMAGE_CAPTION],
        producer_type=MediaProducerType.MODEL_BACKED,
        executable_path=executable_path,
        model_identity=model_identity,
        fixed_args=fixed_args or ["{input_path}", "{output_path}"],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


def _is_strict_caption(caption: str) -> tuple[bool, str | None]:
    """Enforce the strict caption contract (Decision 6): a caption is model
    output describing visual content, never treated as OCR or verified fact.

    Rejects empty/whitespace-only captions, captions over the length bound,
    and captions containing control characters (a sign of a malformed or
    injected payload rather than natural-language description).
    """
    stripped = caption.strip()
    if not stripped:
        return False, "Caption must not be empty"
    if len(stripped) > _CAPTION_MAX_CHARS:
        return False, f"Caption exceeds {_CAPTION_MAX_CHARS} characters"
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in stripped):
        return False, "Caption contains control characters"
    return True, None


class ImageCaptionPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing a strict-contract image caption."""

    def __init__(self) -> None:
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "image_caption_pipeline"

    @classmethod
    def get_adapter_version(cls) -> str:
        return _ADAPTER_VERSION

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return build_caption_pipeline_definition()

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.CPU_ONLY]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.PYTHON_TORCH]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        definition = type(self).get_pipeline_definition()
        output_path = working_directory / "caption.json"
        result = self._executor.execute(
            definition, file_path, working_directory, output_path=output_path
        )
        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Captioning failed (exit={result.exit_code}, timed_out={result.timed_out}): "
                f"{result.stderr_sample}"
            )
        if not output_path.exists():
            raise RuntimeError("Caption tool produced no output file")

        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"Caption output is not valid JSON: {e}") from e

        is_valid, error = validate_json_output(payload, {"caption"}, {"caption": str})
        if not is_valid:
            raise RuntimeError(f"Caption output failed contract validation: {error}")

        caption = payload["caption"]
        caption_ok, caption_error = _is_strict_caption(caption)
        if not caption_ok:
            raise RuntimeError(f"Caption failed strict contract: {caption_error}")

        confidence_raw = payload.get("confidence")
        confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None

        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.IMAGE_CAPTION,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=caption.strip(),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            confidence=confidence,
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
                adapter_name=definition.id,
                adapter_version=_ADAPTER_VERSION,
                model_identity=definition.model_identity,
                model_version=pipeline_fingerprint.model_version,
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: object, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Output is not a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.IMAGE_CAPTION:
            return False, f"Expected IMAGE_CAPTION, got {output.kind}"
        if output.status != MediaRepresentationStatus.CURRENT:
            return True, None
        if not output.textual_payload:
            return False, "CURRENT caption representation missing textual_payload"
        return _is_strict_caption(output.textual_payload)


# =============================================================================
# Task 5.5: optional visual embedding generation
# =============================================================================


def build_embedding_pipeline_definition(
    executable_path: str | None = None,
    *,
    id: str = "image_visual_embedding_v1",  # noqa: A002
    model_identity: str | None = None,
    model_version: str | None = None,
    fixed_args: list[str] | None = None,
    timeout_seconds: float = 60.0,
    max_output_bytes: int = 1_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for the optional visual embedding pipeline.

    The configured executable wraps a local compatible encoder and writes a
    JSON document to `output_path` with required `embedding` (list of
    floats) and `space` (embedding space identifier) keys.
    """
    return MediaPipelineDefinition(
        id=id,
        name="Local Visual Embedding",
        description="Optional visual embedding generation through a configured local encoder.",
        stage=PipelineStage.EMBED_VISUAL,
        accepted_mime_patterns=["image/*"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.VISUAL_EMBEDDING],
        producer_type=MediaProducerType.MODEL_BACKED,
        executable_path=executable_path,
        model_identity=model_identity,
        fixed_args=fixed_args or ["{input_path}", "{output_path}"],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        strict_output_contract=True,
        retry_on_failure=True,
    )


class ImageVisualEmbeddingPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter producing a compatible visual embedding."""

    def __init__(self) -> None:
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return "image_visual_embedding_pipeline"

    @classmethod
    def get_adapter_version(cls) -> str:
        return _ADAPTER_VERSION

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return build_embedding_pipeline_definition()

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.CPU_ONLY]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.PYTHON_TORCH]

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        definition = type(self).get_pipeline_definition()
        output_path = working_directory / "embedding.json"
        result = self._executor.execute(
            definition, file_path, working_directory, output_path=output_path
        )
        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Embedding failed (exit={result.exit_code}, timed_out={result.timed_out}): "
                f"{result.stderr_sample}"
            )
        if not output_path.exists():
            raise RuntimeError("Embedding tool produced no output file")

        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"Embedding output is not valid JSON: {e}") from e

        is_valid, error = validate_json_output(
            payload, {"embedding", "space"}, {"embedding": list, "space": str}
        )
        if not is_valid:
            raise RuntimeError(f"Embedding output failed contract validation: {error}")

        embedding = payload["embedding"]
        if not embedding or len(embedding) > _MAX_EMBEDDING_DIMENSION:
            raise RuntimeError(
                f"Embedding dimension {len(embedding)} out of bounds "
                f"[1, {_MAX_EMBEDDING_DIMENSION}]"
            )
        if not all(isinstance(v, (int, float)) for v in embedding):
            raise RuntimeError("Embedding vector contains non-numeric values")

        space = payload["space"]
        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.VISUAL_EMBEDDING,
            media_type="application/json",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=json.dumps(
                {"embedding": [float(v) for v in embedding], "space": space}
            ),
            locators=(
                WholeResourceLocator(
                    resource_version_id=resource_version_id, representation_id=rep_id
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.MODEL_BACKED,
                adapter_name=definition.id,
                adapter_version=_ADAPTER_VERSION,
                model_identity=definition.model_identity,
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(
        self, output: object, representation_kind: MediaRepresentationKind
    ) -> tuple[bool, str | None]:
        if not isinstance(output, DerivedRepresentation):
            return False, "Output is not a DerivedRepresentation"
        if output.kind != MediaRepresentationKind.VISUAL_EMBEDDING:
            return False, f"Expected VISUAL_EMBEDDING, got {output.kind}"
        if output.status != MediaRepresentationStatus.CURRENT:
            return True, None
        if not output.textual_payload:
            return False, "CURRENT embedding representation missing textual_payload"
        try:
            parsed = json.loads(output.textual_payload)
        except json.JSONDecodeError:
            return False, "Embedding textual_payload is not valid JSON"
        if not isinstance(parsed, dict) or "embedding" not in parsed or "space" not in parsed:
            return False, "Embedding payload missing required keys"
        if not isinstance(parsed["embedding"], list) or not parsed["embedding"]:
            return False, "Embedding vector must be a non-empty list"
        return True, None
