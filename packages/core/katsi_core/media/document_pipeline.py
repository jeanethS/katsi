"""Scanned-document understanding: text-sufficiency, bounded page rendering, and OCR.

Implements openspec change `multimedia-understanding` section 6
(Scanned-Document Understanding, design.md Decision 9):

1. Normal document text extraction (markitdown, see `katsi_core.ingest.extract`)
   remains first. A configured text-sufficiency policy decides whether native
   extracted text is enough, using page count, extracted character coverage,
   and image-only page evidence (task 6.1).
2. When insufficient, bounded local page rendering produces page raster
   images without ever replacing or mutating the source document (task 6.2).
   Rendering is a `MediaPipelineDefinition` executed exclusively through
   `BoundedSubprocessExecutor`/`PipelineExecutionOrchestrator` -- this module
   never invokes a PDF renderer binary directly.
3. Each rendered page image is handed to whatever pipeline is registered to
   produce `MediaRepresentationKind.OCR_TEXT` for image MIME types -- i.e.
   the image OCR pipeline from section 5, reused through the same
   `MediaPipelineRegistry` + `PipelineExecutionOrchestrator` contract every
   other pipeline uses (task 6.3). This module makes no assumption about the
   OCR adapter's internals, only about the shared `MediaPipelineProtocol`
   contract, so it does not need to import anything from section 5's module.
4. Native extracted text and OCR text remain distinguishable
   (`MediaRepresentationKind.EXTRACTED_TEXT` vs `OCR_TEXT`); overlapping
   passages get deduplication evidence rather than a destructive merge
   (task 6.4).
5. Encrypted, password-protected, oversized, and unrenderable documents
   produce an `UNAVAILABLE` representation with structured error information
   instead of attempting to process them (task 6.5).

Reconciliation note for section 5: this module resolves the OCR pipeline via
`MediaPipelineRegistry.resolve(mime_type, MediaRepresentationKind.OCR_TEXT)`
and drives it with `PipelineExecutionOrchestrator.run(...)`, exactly like any
other caller of a registered pipeline. As long as section 5 registers an
adapter that accepts `image/png` (or whatever raster MIME the configured
renderer emits) and produces `OCR_TEXT`, no further coordination is needed.
"""

from __future__ import annotations

import re
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from katsi_core.media.blob_store import BlobStore
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    ImageRegionLocator,
    MediaCoverage,
    MediaDescriptor,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PageLocator,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.detection import ContentSignatureDetector
from katsi_core.media.execution import BoundedSubprocessExecutor, PipelineExecutionOrchestrator
from katsi_core.media.pipeline_registry import MediaPipelineRegistry, PipelineNotFoundError
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

_ADAPTER_NAME = "pdf_page_render"
_ADAPTER_VERSION = "1.0.0"
_PAGE_RENDER_STAGE = PipelineStage.GENERATE_PROXY
_PAGE_RENDERED_MIME = "image/png"

# Bounded scan for page counting: same philosophy as detection.py's
# `_looks_pdf_encrypted` -- a bounded prefix read, never the whole file.
_MAX_PDF_STRUCTURE_SCAN_BYTES = 2_000_000
_PDF_PAGE_OBJECT_RE = re.compile(rb"/Type\s*/Page(?!s)\b")

# Rendered page filenames follow "<prefix>-<n>.png" (poppler's pdftoppm
# convention when given an output prefix and `-png`).
_RENDERED_PAGE_RE = re.compile(r"-(\d+)\.png$")


# =============================================================================
# Task 6.1: configured text-sufficiency evaluation
# =============================================================================


class DocumentTextSufficiencyThresholds(BaseModel):
    """Owner-configured thresholds for the text-sufficiency policy (task 6.1)."""

    min_chars_per_page: int = Field(
        default=200, ge=1, description="Minimum extracted characters expected per page"
    )
    min_coverage_fraction: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Minimum fraction of pages considered text-sufficient",
    )
    max_pages_for_ocr: int = Field(
        default=50, ge=1, description="Maximum page count eligible for bounded OCR fallback"
    )


@dataclass(frozen=True)
class TextSufficiencyResult:
    """Outcome of evaluating whether native extracted text is sufficient."""

    sufficient: bool
    coverage_fraction: float
    image_only_pages: tuple[int, ...]
    detail: str


def evaluate_text_sufficiency(
    page_count: int,
    extracted_text: str,
    thresholds: DocumentTextSufficiencyThresholds,
    native_text_by_page: dict[int, str] | None = None,
) -> TextSufficiencyResult:
    """Evaluate whether native extracted text covers the document adequately.

    Uses page count, extracted character coverage, and image-only page
    evidence (design.md Decision 9 / task 6.1). When `native_text_by_page`
    is supplied (one-based page number -> native text for that page), each
    page is evaluated individually so image-only pages can be localized
    precisely (used for hybrid documents). Otherwise this falls back to an
    average-characters-per-page heuristic over the whole extracted blob,
    since single-pass extractors (e.g. markitdown) do not expose page
    boundaries; in that case every page is conservatively treated as
    image-only whenever the average falls below the threshold, since we
    cannot localize which specific pages are short on text.
    """
    if page_count <= 0:
        return TextSufficiencyResult(
            sufficient=False,
            coverage_fraction=0.0,
            image_only_pages=(),
            detail="Document has no pages",
        )

    if native_text_by_page is not None:
        image_only: list[int] = []
        for page_number in range(1, page_count + 1):
            page_text = native_text_by_page.get(page_number, "")
            if len(page_text.strip()) < thresholds.min_chars_per_page:
                image_only.append(page_number)
        coverage_fraction = 1.0 - (len(image_only) / page_count)
    else:
        average_chars = len(extracted_text) / page_count
        coverage_fraction = min(1.0, average_chars / thresholds.min_chars_per_page)
        image_only = list(range(1, page_count + 1)) if coverage_fraction < 1.0 else []

    sufficient = coverage_fraction >= thresholds.min_coverage_fraction
    detail = (
        f"coverage={coverage_fraction:.2f} over {page_count} page(s), "
        f"{len(image_only)} flagged image-only"
    )
    return TextSufficiencyResult(
        sufficient=sufficient,
        coverage_fraction=coverage_fraction,
        image_only_pages=tuple(image_only),
        detail=detail,
    )


# =============================================================================
# Task 6.5: unavailable-state detection (encrypted/oversized/unrenderable)
# =============================================================================


class DocumentUnavailableReason(BaseModel):
    """Structured reason a document cannot be processed further."""

    error_category: str
    error_message: str


def count_pdf_pages(file_path: Path) -> int | None:
    """Bounded scan for `/Type /Page` object counts in a PDF.

    Reads at most `_MAX_PDF_STRUCTURE_SCAN_BYTES`, mirroring
    `detection._looks_pdf_encrypted`'s bounded-scan philosophy. Returns
    `None` if no page objects are found within the scanned window (the
    caller should treat this as "unknown", not zero).
    """
    try:
        with file_path.open("rb") as handle:
            data = handle.read(_MAX_PDF_STRUCTURE_SCAN_BYTES)
    except OSError:
        return None

    count = len(_PDF_PAGE_OBJECT_RE.findall(data))
    return count or None


def check_document_availability(
    file_path: Path,
    content_hash: ContentHash,
    thresholds: DocumentTextSufficiencyThresholds,
    detector: ContentSignatureDetector | None = None,
) -> tuple[MediaDescriptor, DocumentUnavailableReason | None]:
    """Reuse section 3's content-signature detection to gate document processing.

    Returns the detected `MediaDescriptor` plus a reason the document is
    unavailable for OCR fallback (encrypted, password-protected, oversized,
    or unrenderable), or `None` when processing may proceed. This never
    reimplements PDF encryption detection -- it reuses
    `ContentSignatureDetector`, which already inspects `/Encrypt` (task 6.5).
    """
    detector = detector or ContentSignatureDetector()
    descriptor = detector.detect_media(file_path, content_hash)

    if descriptor.malformed:
        return descriptor, DocumentUnavailableReason(
            error_category="unrenderable",
            error_message="Document content is malformed or unreadable",
        )

    if descriptor.encrypted or descriptor.password_protected:
        return descriptor, DocumentUnavailableReason(
            error_category="encrypted",
            error_message="Document is encrypted or password-protected",
        )

    page_count = descriptor.page_count or count_pdf_pages(file_path)
    if page_count is not None and page_count > thresholds.max_pages_for_ocr:
        return descriptor, DocumentUnavailableReason(
            error_category="oversized",
            error_message=(
                f"Document has {page_count} pages, exceeding the configured "
                f"max_pages_for_ocr={thresholds.max_pages_for_ocr}"
            ),
        )

    return descriptor, None


def build_unavailable_representation(
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    kind: MediaRepresentationKind,
    reason: DocumentUnavailableReason,
    adapter_name: str = _ADAPTER_NAME,
) -> DerivedRepresentation:
    """Build a structured UNAVAILABLE representation for a gated document (task 6.5)."""
    now = datetime.now(UTC)
    textual_payload = "" if kind in _TEXT_KINDS else None
    blob_reference = None if kind in _TEXT_KINDS else "unavailable"
    blob_hash = None if kind in _TEXT_KINDS else "0" * 32
    blob_byte_count = None if kind in _TEXT_KINDS else 0

    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=kind,
        media_type="application/octet-stream",
        status=MediaRepresentationStatus.UNAVAILABLE,
        created_at=now,
        updated_at=now,
        textual_payload=textual_payload,
        blob_reference=blob_reference,
        blob_hash=blob_hash,
        blob_byte_count=blob_byte_count,
        coverage=MediaCoverage(
            is_complete=False, coverage_fraction=0.0, detail=reason.error_message
        ),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name=adapter_name,
            adapter_version=_ADAPTER_VERSION,
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=source_content_hash,
            representation_kind=kind,
            stage=PipelineStage.OCR,
            adapter_name=adapter_name,
            adapter_version=_ADAPTER_VERSION,
            sampling_fingerprint="document_unavailable_v1",
        ),
        error=RepresentationError(
            error_category=reason.error_category,
            error_message=reason.error_message,
            is_retriable=False,
        ),
    )


_TEXT_KINDS = {
    MediaRepresentationKind.EXTRACTED_TEXT,
    MediaRepresentationKind.OCR_TEXT,
    MediaRepresentationKind.IMAGE_CAPTION,
    MediaRepresentationKind.TRANSCRIPT_SEGMENT,
}


# =============================================================================
# Task 6.2: bounded local page rendering (subprocess adapter, never in-process)
# =============================================================================


def build_page_render_pipeline_definition(
    executable_path: str | None,
    *,
    dpi: int = 150,
    timeout_seconds: float = 60.0,
    max_pages: int = 50,
    max_output_bytes: int = 200_000_000,
) -> MediaPipelineDefinition:
    """Owner-configured definition for the bounded page-rendering pipeline.

    `executable_path` is owner-supplied (e.g. a `pdftoppm` binary path) and
    optional/lazy: if unset, the pipeline reports itself unavailable via
    `check_availability` and this module falls back to an UNAVAILABLE
    representation rather than attempting to render.

    Renders every page up to `max_pages` in a single bounded invocation
    (poppler's `pdftoppm -png <input> <output-prefix>` renders one file per
    page); the fixed `-l` bound below caps that at `max_pages` so a
    maliciously large PDF cannot make a single invocation produce unbounded
    output. Only `input_path`/`output_path`/`working_directory` are ever
    substituted (see `execution.ALLOWED_ARG_PLACEHOLDERS`); page selection
    is a static, owner-authored flag, never an agent-supplied value.
    """
    return MediaPipelineDefinition(
        id="pdf_page_render_v1",
        name="Bounded PDF Page Renderer",
        description=(
            "Renders bounded PNG page images from a PDF for OCR fallback, "
            "without replacing or mutating the source document."
        ),
        stage=_PAGE_RENDER_STAGE,
        accepted_mime_patterns=["application/pdf"],
        input_kinds=[],
        representation_kinds_produced=[MediaRepresentationKind.PROXY_MEDIA],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=executable_path,
        fixed_args=["-png", "-r", str(dpi), "-l", str(max_pages), "{input_path}", "{output_path}"],
        allowed_env_vars=[],
        working_directory=".",
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=timeout_seconds,
        max_memory_mb=None,
        max_output_bytes=max_output_bytes,
        max_pages=max_pages,
        strict_output_contract=True,
        retry_on_failure=True,
        availability_probe=None,
        required_hardware=[],
    )


class PdfPageRenderPipeline(MediaPipelineProtocol):
    """Bounded subprocess adapter that renders PDF pages to a PNG bundle.

    Produces a single `PROXY_MEDIA` representation whose blob is a tar
    archive of `page-<n>.png` files (one-based page numbers preserved in
    the filename). This module never replaces or writes back to the source
    document -- rendered images are private derived artifacts stored in the
    blob store. Only `BoundedSubprocessExecutor` ever invokes the renderer
    binary; this class never calls `subprocess` itself.
    """

    def __init__(self, definition: MediaPipelineDefinition, blob_store: BlobStore) -> None:
        self._definition = definition
        self._blob_store = blob_store
        self._executor = BoundedSubprocessExecutor()

    @classmethod
    def get_adapter_name(cls) -> str:
        return _ADAPTER_NAME

    @classmethod
    def get_adapter_version(cls) -> str:
        return _ADAPTER_VERSION

    def get_pipeline_definition(self) -> MediaPipelineDefinition:  # type: ignore[override]
        return self._definition

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.POPPLER]

    def check_availability(self) -> tuple[bool, str | None]:  # type: ignore[override]
        """Instance-level availability check.

        Overrides the base classmethod because this adapter's definition is
        owner-configured per instance (`executable_path` is not fixed class
        state) rather than a stateless class attribute. Not routed through
        `MediaPipelineRegistry.available_pipeline_ids`, which assumes a
        zero-arg classmethod; this adapter is constructed and probed
        directly by `DocumentUnderstandingPipeline`.
        """
        if not self._definition.executable_path:
            return False, "No page renderer executable configured (e.g. pdftoppm)"
        return self._check_software_dependency(SoftwareDependency.POPPLER)

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        output_prefix = working_directory / "page"
        result = self._executor.execute(
            self._definition, file_path, working_directory, output_path=output_prefix
        )
        if result.timed_out or result.exit_code != 0:
            raise RuntimeError(
                f"Page render failed (exit={result.exit_code}, timed_out={result.timed_out}): "
                f"{result.stderr_sample}"
            )

        rendered = sorted(
            working_directory.glob("page-*.png"),
            key=lambda p: int(_RENDERED_PAGE_RE.search(p.name).group(1))  # type: ignore[union-attr]
            if _RENDERED_PAGE_RE.search(p.name)
            else 0,
        )
        if not rendered:
            raise RuntimeError("Page renderer produced no output pages")

        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for page_path in rendered:
                archive.add(page_path, arcname=page_path.name)
        bundle_bytes = buffer.getvalue()

        blob_hash, byte_count = self._blob_store.store_blob(bundle_bytes)
        now = datetime.now(UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.PROXY_MEDIA,
            media_type="application/x-tar",
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
                detail=f"Rendered {len(rendered)} page(s)",
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
        if output.kind != MediaRepresentationKind.PROXY_MEDIA:
            return False, f"Expected PROXY_MEDIA, got {output.kind}"
        if output.status == MediaRepresentationStatus.CURRENT and not output.blob_reference:
            return False, "CURRENT proxy representation missing blob_reference"
        return True, None


def _unpack_rendered_pages(bundle_bytes: bytes, destination: Path) -> dict[int, Path]:
    """Extract a page-render tar bundle, returning page_number -> image path."""
    pages: dict[int, Path] = {}
    with tarfile.open(fileobj=BytesIO(bundle_bytes), mode="r") as archive:
        archive.extractall(destination, filter="data")  # noqa: S202 -- trusted, self-produced bundle
        for member in archive.getmembers():
            match = _RENDERED_PAGE_RE.search(member.name)
            if match is not None:
                pages[int(match.group(1))] = destination / member.name
    return pages


# =============================================================================
# Task 6.3/6.4: reuse the image OCR pipeline, attach page locators, dedup
# =============================================================================


@dataclass(frozen=True)
class PageDeduplicationEvidence:
    """Overlap evidence between a page's OCR text and the native extracted text.

    Kept as document-pipeline-local evidence (task 6.4) rather than a new
    field on the shared `DerivedRepresentation` contract, since that model
    is owned jointly across sections; overlap is exposed here for callers
    (e.g. retrieval/citation code) to decide how to treat duplicate
    passages without a destructive merge.
    """

    page_number: int
    overlap_ratio: float
    likely_duplicate: bool


def _text_overlap_ratio(a: str, b: str) -> float:
    """Longest-matching-block overlap ratio between two text blobs (stdlib difflib)."""
    if not a.strip() or not b.strip():
        return 0.0
    return SequenceMatcher(a=a, b=b, autojunk=False).ratio()


@dataclass(frozen=True)
class DocumentOcrOutcome:
    """Result of running bounded OCR fallback across a document's pages."""

    page_representations: tuple[DerivedRepresentation, ...]
    dedup_evidence: tuple[PageDeduplicationEvidence, ...]


class DocumentOcrCoordinator:
    """Reuses the registered image OCR pipeline to recognize text on rendered pages.

    This coordinator makes no assumption about section 5's OCR adapter
    beyond the shared `MediaPipelineProtocol`/`MediaPipelineRegistry`
    contract: it resolves whatever pipeline is registered to produce
    `OCR_TEXT` for the rendered page MIME type and drives it through
    `PipelineExecutionOrchestrator`, exactly like any other pipeline
    consumer. If section 5 is not registered yet, `run_ocr_on_pages` raises
    `PipelineNotFoundError`, which callers should treat as "OCR fallback
    unavailable" rather than a hard failure of document understanding.
    """

    def __init__(
        self,
        registry: MediaPipelineRegistry,
        orchestrator: PipelineExecutionOrchestrator | None = None,
        rendered_mime_type: str = _PAGE_RENDERED_MIME,
        dedup_overlap_threshold: float = 0.6,
    ) -> None:
        self._registry = registry
        self._orchestrator = orchestrator or PipelineExecutionOrchestrator()
        self._rendered_mime_type = rendered_mime_type
        self._dedup_overlap_threshold = dedup_overlap_threshold

    def run_ocr_on_pages(
        self,
        page_images: dict[int, Path],
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        sampling_fingerprint: str,
        working_directory: Path,
        native_extracted_text: str = "",
    ) -> DocumentOcrOutcome:
        """Run the registered OCR_TEXT pipeline against each rendered page image.

        Raises:
            PipelineNotFoundError: No pipeline is registered to produce
                `OCR_TEXT` for `rendered_mime_type`. Reconciliation: section
                5 must register such a pipeline for this to succeed.
        """
        registered = self._registry.resolve(
            self._rendered_mime_type, MediaRepresentationKind.OCR_TEXT
        )
        if registered is None or registered.adapter_class is None:
            raise PipelineNotFoundError(
                f"No OCR pipeline registered for mime type '{self._rendered_mime_type}'"
            )
        adapter = registered.adapter_class()
        definition = registered.definition

        page_reps: list[DerivedRepresentation] = []
        dedup: list[PageDeduplicationEvidence] = []

        for page_number in sorted(page_images):
            image_path = page_images[page_number]
            fingerprint = PipelineFingerprint(
                source_content_hash=source_content_hash,
                representation_kind=MediaRepresentationKind.OCR_TEXT,
                stage=PipelineStage.OCR,
                adapter_name=definition.id,
                adapter_version=_ADAPTER_VERSION,
                sampling_fingerprint=sampling_fingerprint,
            )
            image_working_dir = working_directory / f"ocr-page-{page_number}"
            image_working_dir.mkdir(parents=True, exist_ok=True)

            raw_ocr = self._orchestrator.run(
                adapter,
                definition,
                image_path,
                resource_version_id,
                source_content_hash,
                fingerprint,
            )
            page_rep = _attach_page_locator(raw_ocr, page_number)
            page_reps.append(page_rep)

            if page_rep.status == MediaRepresentationStatus.CURRENT and page_rep.textual_payload:
                overlap = _text_overlap_ratio(page_rep.textual_payload, native_extracted_text)
                dedup.append(
                    PageDeduplicationEvidence(
                        page_number=page_number,
                        overlap_ratio=overlap,
                        likely_duplicate=overlap >= self._dedup_overlap_threshold,
                    )
                )

        return DocumentOcrOutcome(
            page_representations=tuple(page_reps), dedup_evidence=tuple(dedup)
        )


def _attach_page_locator(
    representation: DerivedRepresentation, page_number: int
) -> DerivedRepresentation:
    """Rewrite an image-scoped OCR representation's locators as page locators.

    The OCR pipeline reasons about a single rendered page image and so
    returns image-region or whole-resource locators. This document layer
    knows which page that image came from, so it remaps each locator to a
    one-based `PageLocator` carrying the same normalized bounding box where
    the OCR adapter supplied one (task 6.3).
    """
    new_locators: list[PageLocator] = []
    for locator in representation.locators:
        bounding_box = locator.bounding_box if isinstance(locator, ImageRegionLocator) else None
        new_locators.append(
            PageLocator(
                resource_version_id=locator.resource_version_id,
                representation_id=locator.representation_id,
                page_number=page_number,
                bounding_box=bounding_box,
            )
        )
    if not new_locators:
        new_locators.append(
            PageLocator(
                resource_version_id=representation.resource_version_id,
                representation_id=representation.id,
                page_number=page_number,
            )
        )
    return representation.model_copy(update={"locators": tuple(new_locators)})


# =============================================================================
# Top-level orchestration
# =============================================================================


@dataclass(frozen=True)
class DocumentUnderstandingResult:
    """Complete outcome of processing a document for text/OCR representations."""

    native_representation: DerivedRepresentation | None
    sufficiency: TextSufficiencyResult | None
    ocr_outcome: DocumentOcrOutcome | None
    unavailable_reason: DocumentUnavailableReason | None


def build_native_extracted_text_representation(
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    extracted_text: str,
    sampling_fingerprint: str,
    adapter_name: str = "markitdown",
    adapter_version: str = "1.0.0",
) -> DerivedRepresentation:
    """Wrap markitdown-extracted text as a distinguishable EXTRACTED_TEXT representation.

    Kept distinct in `kind` from `OCR_TEXT` (task 6.4) so downstream
    retrieval/citation code always knows whether a passage came from native
    extraction or recognition, even when both cover the same page.
    """
    now = datetime.now(UTC)
    rep_id = uuid4()
    return DerivedRepresentation(
        id=rep_id,
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=extracted_text,
        locators=(
            WholeResourceLocator(resource_version_id=resource_version_id, representation_id=rep_id),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            sampling_fingerprint=sampling_fingerprint,
        ),
    )


@dataclass
class DocumentUnderstandingPipeline:
    """Coordinates text-sufficiency evaluation, bounded rendering, and OCR reuse.

    This is the single entry point sections outside `media/` should call for
    scanned-document understanding; it composes the pieces above (tasks
    6.1-6.5) and never bypasses the shared detection/execution/registry
    contracts owned by earlier sections.
    """

    thresholds: DocumentTextSufficiencyThresholds = field(
        default_factory=DocumentTextSufficiencyThresholds
    )
    blob_store: BlobStore | None = None
    registry: MediaPipelineRegistry | None = None
    render_executable_path: str | None = None
    detector: ContentSignatureDetector | None = None

    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        extracted_text: str,
        sampling_fingerprint: str,
        working_directory: Path,
        native_text_by_page: dict[int, str] | None = None,
    ) -> DocumentUnderstandingResult:
        descriptor, unavailable = check_document_availability(
            file_path, source_content_hash, self.thresholds, self.detector
        )
        if unavailable is not None:
            return DocumentUnderstandingResult(
                native_representation=None,
                sufficiency=None,
                ocr_outcome=None,
                unavailable_reason=unavailable,
            )

        native_representation = build_native_extracted_text_representation(
            resource_version_id, source_content_hash, extracted_text, sampling_fingerprint
        )

        page_count = descriptor.page_count or count_pdf_pages(file_path)
        if page_count is None:
            # Cannot evaluate sufficiency without a page count; native text
            # extraction stands alone rather than guessing at OCR fallback.
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=None,
                ocr_outcome=None,
                unavailable_reason=None,
            )

        sufficiency = evaluate_text_sufficiency(
            page_count, extracted_text, self.thresholds, native_text_by_page
        )
        # Gate OCR on whether any *specific* pages were flagged image-only,
        # not on the aggregate `sufficient` verdict: a hybrid document can be
        # sufficient overall while still having individual image-only pages
        # that should retain their own OCR representation (design.md
        # Decision 9: "Hybrid pages may retain both representations").
        if not sufficiency.image_only_pages:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=None,
            )

        if self.blob_store is None or self.registry is None:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=DocumentUnavailableReason(
                    error_category="unrenderable",
                    error_message="No blob store/pipeline registry configured for OCR fallback",
                ),
            )

        render_definition = build_page_render_pipeline_definition(
            self.render_executable_path, max_pages=self.thresholds.max_pages_for_ocr
        )
        render_adapter = PdfPageRenderPipeline(render_definition, self.blob_store)
        available, probe_error = render_adapter.check_availability()
        if not available:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=DocumentUnavailableReason(
                    error_category="unrenderable",
                    error_message=probe_error or "Page renderer unavailable",
                ),
            )

        render_orchestrator = PipelineExecutionOrchestrator()
        render_fingerprint = PipelineFingerprint(
            source_content_hash=source_content_hash,
            representation_kind=MediaRepresentationKind.PROXY_MEDIA,
            stage=_PAGE_RENDER_STAGE,
            adapter_name=render_definition.id,
            adapter_version=_ADAPTER_VERSION,
            sampling_fingerprint=sampling_fingerprint,
        )
        render_representation = render_orchestrator.run(
            render_adapter,
            render_definition,
            file_path,
            resource_version_id,
            source_content_hash,
            render_fingerprint,
        )
        if render_representation.status != MediaRepresentationStatus.CURRENT:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=DocumentUnavailableReason(
                    error_category="unrenderable",
                    error_message=(
                        render_representation.error.error_message
                        if render_representation.error
                        else "Page rendering failed"
                    ),
                ),
            )

        bundle_bytes = (
            self.blob_store.get_blob(render_representation.blob_hash)
            if render_representation.blob_hash
            else None
        )
        if bundle_bytes is None:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=DocumentUnavailableReason(
                    error_category="unrenderable",
                    error_message="Rendered page bundle missing from blob store",
                ),
            )

        unpack_dir = working_directory / "rendered-pages"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        all_pages = _unpack_rendered_pages(bundle_bytes, unpack_dir)
        pages_needing_ocr = {
            page: path
            for page, path in all_pages.items()
            if page in sufficiency.image_only_pages
        }

        coordinator = DocumentOcrCoordinator(self.registry)
        try:
            ocr_outcome = coordinator.run_ocr_on_pages(
                pages_needing_ocr,
                resource_version_id,
                source_content_hash,
                sampling_fingerprint,
                working_directory,
                native_extracted_text=extracted_text,
            )
        except PipelineNotFoundError as e:
            return DocumentUnderstandingResult(
                native_representation=native_representation,
                sufficiency=sufficiency,
                ocr_outcome=None,
                unavailable_reason=DocumentUnavailableReason(
                    error_category="unrenderable", error_message=str(e)
                ),
            )

        return DocumentUnderstandingResult(
            native_representation=native_representation,
            sufficiency=sufficiency,
            ocr_outcome=ocr_outcome,
            unavailable_reason=None,
        )
