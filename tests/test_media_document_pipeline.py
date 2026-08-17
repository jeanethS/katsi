"""Tests for scanned-document understanding (openspec `multimedia-understanding` section 6).

Covers: text-sufficiency evaluation (6.1), bounded local page rendering
through the shared subprocess executor (6.2), reuse of a registered OCR_TEXT
pipeline with page/region locator remapping (6.3), native-vs-OCR
distinguishability and deduplication evidence (6.4), unavailable states for
encrypted/oversized/unrenderable documents (6.5), and fixtures spanning
text-native, image-only, hybrid, rotated-page, encrypted, and
partial-failure documents (6.6).

All PDF fixtures are tiny synthetic byte structures built in-test -- no
external files or binaries. Page rendering is exercised by monkeypatching
`BoundedSubprocessExecutor.execute` to emulate a renderer's side effect
(writing page PNG files) without requiring poppler to be installed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.media.blob_store import BlobStore
from katsi_core.media.contracts import (
    DerivedRepresentation,
    ImageRegionLocator,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PageLocator,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
)
from katsi_core.media.document_pipeline import (
    DocumentOcrCoordinator,
    DocumentTextSufficiencyThresholds,
    DocumentUnderstandingPipeline,
    PdfPageRenderPipeline,
    build_native_extracted_text_representation,
    build_page_render_pipeline_definition,
    build_unavailable_representation,
    check_document_availability,
    count_pdf_pages,
    evaluate_text_sufficiency,
)
from katsi_core.media.execution import BoundedExecutionResult, BoundedSubprocessExecutor
from katsi_core.media.pipeline_registry import MediaPipelineRegistry, PipelineNotFoundError
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

CONTENT_HASH = "a" * 32


def _pdf_bytes(page_count: int, encrypted: bool = False) -> bytes:
    """Build a tiny synthetic PDF byte structure with N page objects."""
    header = b"%PDF-1.4\n"
    if encrypted:
        header += b"/Encrypt 5 0 R\n"
    pages = b"".join(f"{n} 0 obj\n/Type /Page\nendobj\n".encode() for n in range(1, page_count + 1))
    return header + pages + b"%%EOF\n"


def _write_pdf(tmp_path: Path, name: str, page_count: int, encrypted: bool = False) -> Path:
    path = tmp_path / name
    path.write_bytes(_pdf_bytes(page_count, encrypted=encrypted))
    return path


# ---------------------------------------------------------------------------
# Task 6.1: text-sufficiency evaluation
# ---------------------------------------------------------------------------


class TestEvaluateTextSufficiency:
    def test_text_native_document_is_sufficient(self) -> None:
        thresholds = DocumentTextSufficiencyThresholds()
        result = evaluate_text_sufficiency(
            page_count=3, extracted_text="word " * 500, thresholds=thresholds
        )
        assert result.sufficient
        assert result.image_only_pages == ()
        assert result.coverage_fraction == 1.0

    def test_image_only_document_is_insufficient(self) -> None:
        thresholds = DocumentTextSufficiencyThresholds()
        result = evaluate_text_sufficiency(page_count=3, extracted_text="", thresholds=thresholds)
        assert not result.sufficient
        assert result.image_only_pages == (1, 2, 3)
        assert result.coverage_fraction == 0.0

    def test_hybrid_document_localizes_image_only_pages(self) -> None:
        thresholds = DocumentTextSufficiencyThresholds(min_chars_per_page=50)
        native_text_by_page = {
            1: "word " * 30,  # sufficient
            2: "",  # image-only
            3: "word " * 30,  # sufficient
        }
        result = evaluate_text_sufficiency(
            page_count=3,
            extracted_text="word " * 60,
            thresholds=thresholds,
            native_text_by_page=native_text_by_page,
        )
        assert result.image_only_pages == (2,)
        # coverage_fraction = 2/3 >= default min_coverage_fraction (0.6), so the
        # document overall is "sufficient" even though page 2 is flagged.
        assert result.sufficient
        assert result.coverage_fraction == pytest.approx(2 / 3)

    def test_zero_page_document_is_insufficient(self) -> None:
        result = evaluate_text_sufficiency(
            page_count=0, extracted_text="", thresholds=DocumentTextSufficiencyThresholds()
        )
        assert not result.sufficient
        assert result.detail == "Document has no pages"


# ---------------------------------------------------------------------------
# Bounded PDF page counting
# ---------------------------------------------------------------------------


class TestCountPdfPages:
    def test_counts_page_objects(self, tmp_path: Path) -> None:
        path = _write_pdf(tmp_path, "doc.pdf", page_count=4)
        assert count_pdf_pages(path) == 4

    def test_does_not_count_pages_object(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.pdf"
        path.write_bytes(b"%PDF-1.4\n/Type /Pages\nendobj\n")
        assert count_pdf_pages(path) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert count_pdf_pages(tmp_path / "missing.pdf") is None


# ---------------------------------------------------------------------------
# Task 6.5: unavailable-state detection, reusing section 3 detection
# ---------------------------------------------------------------------------


class TestCheckDocumentAvailability:
    def test_encrypted_document_is_unavailable(self, tmp_path: Path) -> None:
        path = _write_pdf(tmp_path, "encrypted.pdf", page_count=2, encrypted=True)
        thresholds = DocumentTextSufficiencyThresholds()
        _descriptor, reason = check_document_availability(path, CONTENT_HASH, thresholds)
        assert reason is not None
        assert reason.error_category == "encrypted"

    def test_oversized_document_is_unavailable(self, tmp_path: Path) -> None:
        path = _write_pdf(tmp_path, "big.pdf", page_count=10)
        thresholds = DocumentTextSufficiencyThresholds(max_pages_for_ocr=5)
        _descriptor, reason = check_document_availability(path, CONTENT_HASH, thresholds)
        assert reason is not None
        assert reason.error_category == "oversized"

    def test_normal_document_is_available(self, tmp_path: Path) -> None:
        path = _write_pdf(tmp_path, "normal.pdf", page_count=2)
        thresholds = DocumentTextSufficiencyThresholds(max_pages_for_ocr=50)
        _descriptor, reason = check_document_availability(path, CONTENT_HASH, thresholds)
        assert reason is None

    def test_missing_file_is_unrenderable(self, tmp_path: Path) -> None:
        thresholds = DocumentTextSufficiencyThresholds()
        _descriptor, reason = check_document_availability(
            tmp_path / "missing.pdf", CONTENT_HASH, thresholds
        )
        assert reason is not None
        assert reason.error_category == "unrenderable"


class TestBuildUnavailableRepresentation:
    def test_produces_unavailable_status_with_error(self) -> None:
        from katsi_core.media.document_pipeline import DocumentUnavailableReason

        rep = build_unavailable_representation(
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            kind=MediaRepresentationKind.OCR_TEXT,
            reason=DocumentUnavailableReason(
                error_category="encrypted", error_message="Document is encrypted"
            ),
        )
        assert rep.status == MediaRepresentationStatus.UNAVAILABLE
        assert rep.error is not None
        assert rep.error.error_category == "encrypted"
        assert rep.textual_payload == ""  # OCR_TEXT is a text kind


# ---------------------------------------------------------------------------
# Task 6.4: native extracted text stays distinguishable from OCR text
# ---------------------------------------------------------------------------


class TestNativeExtractedTextRepresentation:
    def test_kind_is_extracted_text_not_ocr(self) -> None:
        rep = build_native_extracted_text_representation(
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="hello world",
            sampling_fingerprint="v1",
        )
        assert rep.kind == MediaRepresentationKind.EXTRACTED_TEXT
        assert rep.kind != MediaRepresentationKind.OCR_TEXT
        assert rep.status == MediaRepresentationStatus.CURRENT


# ---------------------------------------------------------------------------
# Task 6.2: bounded local page rendering via the shared subprocess executor
# ---------------------------------------------------------------------------


def _fake_render_execute(pages: list[int]):
    """Build a fake `BoundedSubprocessExecutor.execute` that writes page PNGs.

    Emulates pdftoppm's side effect (writing `<prefix>-<n>.png` files) so
    tests never invoke a real renderer binary while still exercising the
    exact code path that consumes `BoundedExecutionResult`.
    """

    def _execute(self, definition, input_path, working_directory, output_path=None):
        working_directory = Path(working_directory)
        for n in pages:
            (working_directory / f"page-{n}.png").write_bytes(f"page-{n}-bytes".encode())
        return BoundedExecutionResult(
            exit_code=0,
            timed_out=False,
            output_truncated=False,
            stdout_sample="",
            stderr_sample="",
            stdout_bytes=0,
            stderr_bytes=0,
            duration_seconds=0.01,
            network_isolation_applied=True,
        )

    return _execute


class TestPdfPageRenderPipeline:
    def test_renders_and_bundles_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _fake_render_execute([1, 2, 3]))
        blob_store = BlobStore(storage_root=tmp_path / "blobs")
        definition = build_page_render_pipeline_definition("fake-pdftoppm", max_pages=10)
        adapter = PdfPageRenderPipeline(definition, blob_store)

        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=3)
        resource_version_id = uuid4()
        fingerprint = PipelineFingerprint(
            source_content_hash=CONTENT_HASH,
            representation_kind=MediaRepresentationKind.PROXY_MEDIA,
            stage=PipelineStage.GENERATE_PROXY,
            adapter_name=definition.id,
            adapter_version="1.0.0",
            sampling_fingerprint="v1",
        )
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        rep = adapter.process(
            pdf_path, resource_version_id, CONTENT_HASH, fingerprint, working_directory
        )

        assert rep.status == MediaRepresentationStatus.CURRENT
        assert rep.kind == MediaRepresentationKind.PROXY_MEDIA
        assert rep.blob_hash is not None
        bundle_bytes = blob_store.get_blob(rep.blob_hash)
        assert bundle_bytes is not None

        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.PROXY_MEDIA)
        assert is_valid, error

    def test_raises_on_nonzero_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _failing_execute(self, definition, input_path, working_directory, output_path=None):
            return BoundedExecutionResult(
                exit_code=1,
                timed_out=False,
                output_truncated=False,
                stdout_sample="",
                stderr_sample="renderer exploded",
                stdout_bytes=0,
                stderr_bytes=0,
                duration_seconds=0.01,
                network_isolation_applied=True,
            )

        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _failing_execute)
        blob_store = BlobStore(storage_root=tmp_path / "blobs")
        definition = build_page_render_pipeline_definition("fake-pdftoppm")
        adapter = PdfPageRenderPipeline(definition, blob_store)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=1)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        with pytest.raises(RuntimeError):
            adapter.process(
                pdf_path,
                uuid4(),
                CONTENT_HASH,
                PipelineFingerprint(
                    source_content_hash=CONTENT_HASH,
                    representation_kind=MediaRepresentationKind.PROXY_MEDIA,
                    stage=PipelineStage.GENERATE_PROXY,
                    adapter_name=definition.id,
                    adapter_version="1.0.0",
                    sampling_fingerprint="v1",
                ),
                working_directory,
            )

    def test_check_availability_false_without_executable(self, tmp_path: Path) -> None:
        blob_store = BlobStore(storage_root=tmp_path / "blobs")
        definition = build_page_render_pipeline_definition(None)
        adapter = PdfPageRenderPipeline(definition, blob_store)
        available, error = adapter.check_availability()
        assert not available
        assert error is not None


# ---------------------------------------------------------------------------
# Task 6.3/6.4: reusing a registered OCR_TEXT pipeline, page locator remap,
# and deduplication evidence
# ---------------------------------------------------------------------------


class FakeImageOcrPipeline(MediaPipelineProtocol):
    """Stand-in for section 5's image OCR pipeline.

    Only implements the shared `MediaPipelineProtocol` contract -- the
    document pipeline never imports this class or anything like it, it only
    resolves whatever is registered for `OCR_TEXT` against `image/png`.
    """

    FAIL_ON_PAGE_TEXT: str | None = None

    @classmethod
    def get_adapter_name(cls) -> str:
        return "fake_image_ocr"

    @classmethod
    def get_adapter_version(cls) -> str:
        return "1.0.0"

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return MediaPipelineDefinition(
            id="fake_image_ocr_v1",
            name="Fake Image OCR",
            stage=PipelineStage.OCR,
            accepted_mime_patterns=["image/png"],
            representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
            producer_type=MediaProducerType.DETERMINISTIC,
            executable_path="/bin/true",
            fixed_args=[],
            retry_on_failure=False,
        )

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ) -> DerivedRepresentation:
        content = file_path.read_text()
        if content == self.FAIL_ON_PAGE_TEXT:
            raise RuntimeError("simulated OCR failure")

        now = __import__("datetime").datetime.now(__import__("datetime").UTC)
        rep_id = uuid4()
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=now,
            updated_at=now,
            textual_payload=f"recognized:{content}",
            locators=(
                ImageRegionLocator(
                    resource_version_id=resource_version_id,
                    representation_id=rep_id,
                    bounding_box=(0.1, 0.2, 0.3, 0.4),
                ),
            ),
            coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="fake_image_ocr",
                adapter_version="1.0.0",
            ),
            pipeline_fingerprint=pipeline_fingerprint,
        )

    def validate_output(self, output, representation_kind):
        if not isinstance(output, DerivedRepresentation):
            return False, "not a representation"
        return True, None


class TestDocumentOcrCoordinator:
    def test_attaches_page_locator_with_region(self, tmp_path: Path) -> None:
        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)

        page1 = tmp_path / "page-1.png"
        page1.write_text("page-1-bytes")
        page2 = tmp_path / "page-2.png"
        page2.write_text("page-2-bytes")

        coordinator = DocumentOcrCoordinator(registry)
        outcome = coordinator.run_ocr_on_pages(
            {1: page1, 2: page2},
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            sampling_fingerprint="v1",
            working_directory=tmp_path,
            native_extracted_text="unrelated native text",
        )

        assert len(outcome.page_representations) == 2
        by_page = {}
        for rep in outcome.page_representations:
            locator = rep.locators[0]
            assert isinstance(locator, PageLocator)
            by_page[locator.page_number] = (rep, locator)

        assert set(by_page) == {1, 2}
        rep1, locator1 = by_page[1]
        assert locator1.bounding_box == (0.1, 0.2, 0.3, 0.4)
        assert rep1.textual_payload == "recognized:page-1-bytes"
        assert rep1.kind == MediaRepresentationKind.OCR_TEXT

    def test_rotated_page_region_locator_passthrough(self, tmp_path: Path) -> None:
        """A rotated page's OCR region (e.g. a wide box on a portrait page) survives remap."""
        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
        page = tmp_path / "page-1.png"
        page.write_text("page-1-bytes")

        coordinator = DocumentOcrCoordinator(registry)
        outcome = coordinator.run_ocr_on_pages(
            {1: page},
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            sampling_fingerprint="v1",
            working_directory=tmp_path,
        )
        locator = outcome.page_representations[0].locators[0]
        assert isinstance(locator, PageLocator)
        assert locator.page_number == 1
        assert locator.bounding_box is not None

    def test_dedup_evidence_flags_overlapping_native_text(self, tmp_path: Path) -> None:
        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
        page = tmp_path / "page-1.png"
        page.write_text("shared duplicate passage")

        coordinator = DocumentOcrCoordinator(registry, dedup_overlap_threshold=0.3)
        outcome = coordinator.run_ocr_on_pages(
            {1: page},
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            sampling_fingerprint="v1",
            working_directory=tmp_path,
            native_extracted_text="recognized:shared duplicate passage",
        )
        assert len(outcome.dedup_evidence) == 1
        evidence = outcome.dedup_evidence[0]
        assert evidence.page_number == 1
        assert evidence.likely_duplicate

    def test_partial_failure_page_still_returns_representation(self, tmp_path: Path) -> None:
        FakeImageOcrPipeline.FAIL_ON_PAGE_TEXT = "page-2-bytes"
        try:
            registry = MediaPipelineRegistry()
            registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
            page1 = tmp_path / "page-1.png"
            page1.write_text("page-1-bytes")
            page2 = tmp_path / "page-2.png"
            page2.write_text("page-2-bytes")

            coordinator = DocumentOcrCoordinator(registry)
            outcome = coordinator.run_ocr_on_pages(
                {1: page1, 2: page2},
                resource_version_id=uuid4(),
                source_content_hash=CONTENT_HASH,
                sampling_fingerprint="v1",
                working_directory=tmp_path,
            )
            statuses = {
                rep.locators[0].page_number: rep.status  # type: ignore[union-attr]
                for rep in outcome.page_representations
            }
            assert statuses[1] == MediaRepresentationStatus.CURRENT
            assert statuses[2] == MediaRepresentationStatus.FAILED
        finally:
            FakeImageOcrPipeline.FAIL_ON_PAGE_TEXT = None

    def test_missing_ocr_pipeline_raises_not_found(self, tmp_path: Path) -> None:
        registry = MediaPipelineRegistry()
        coordinator = DocumentOcrCoordinator(registry)
        page = tmp_path / "page-1.png"
        page.write_text("x")
        with pytest.raises(PipelineNotFoundError):
            coordinator.run_ocr_on_pages(
                {1: page},
                resource_version_id=uuid4(),
                source_content_hash=CONTENT_HASH,
                sampling_fingerprint="v1",
                working_directory=tmp_path,
            )


# ---------------------------------------------------------------------------
# End-to-end DocumentUnderstandingPipeline (6.1-6.5 composed)
# ---------------------------------------------------------------------------


class TestDocumentUnderstandingPipelineEndToEnd:
    def _pipeline(
        self, tmp_path: Path, registry: MediaPipelineRegistry
    ) -> DocumentUnderstandingPipeline:
        return DocumentUnderstandingPipeline(
            thresholds=DocumentTextSufficiencyThresholds(
                min_chars_per_page=50, max_pages_for_ocr=20
            ),
            blob_store=BlobStore(storage_root=tmp_path / "blobs"),
            registry=registry,
            render_executable_path="fake-pdftoppm",
        )

    def test_text_native_document_skips_ocr(self, tmp_path: Path) -> None:
        registry = MediaPipelineRegistry()
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=2)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="word " * 200,
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        assert result.unavailable_reason is None
        assert result.native_representation is not None
        assert result.native_representation.kind == MediaRepresentationKind.EXTRACTED_TEXT
        assert result.sufficiency is not None
        assert result.sufficiency.sufficient
        assert result.ocr_outcome is None

    def test_image_only_document_triggers_ocr_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "katsi_core.media.document_pipeline.PdfPageRenderPipeline.check_availability",
            lambda self: (True, None),
        )
        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _fake_render_execute([1, 2]))

        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=2)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="",
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        assert result.unavailable_reason is None
        assert not result.sufficiency.sufficient
        assert result.ocr_outcome is not None
        assert len(result.ocr_outcome.page_representations) == 2
        for rep in result.ocr_outcome.page_representations:
            assert rep.kind == MediaRepresentationKind.OCR_TEXT
            assert rep.kind != result.native_representation.kind

    def test_hybrid_document_only_ocrs_image_only_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "katsi_core.media.document_pipeline.PdfPageRenderPipeline.check_availability",
            lambda self: (True, None),
        )
        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _fake_render_execute([1, 2, 3]))

        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=3)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="word " * 30,
            sampling_fingerprint="v1",
            working_directory=working_directory,
            native_text_by_page={1: "word " * 30, 2: "", 3: "word " * 30},
        )

        assert result.sufficiency.image_only_pages == (2,)
        assert result.ocr_outcome is not None
        assert len(result.ocr_outcome.page_representations) == 1
        locator = result.ocr_outcome.page_representations[0].locators[0]
        assert isinstance(locator, PageLocator)
        assert locator.page_number == 2

    def test_encrypted_document_is_unavailable(self, tmp_path: Path) -> None:
        registry = MediaPipelineRegistry()
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "encrypted.pdf", page_count=2, encrypted=True)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="",
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        assert result.native_representation is None
        assert result.unavailable_reason is not None
        assert result.unavailable_reason.error_category == "encrypted"

    def test_renderer_failure_produces_unrenderable_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "katsi_core.media.document_pipeline.PdfPageRenderPipeline.check_availability",
            lambda self: (True, None),
        )

        def _failing_execute(self, definition, input_path, working_directory, output_path=None):
            return BoundedExecutionResult(
                exit_code=1,
                timed_out=False,
                output_truncated=False,
                stdout_sample="",
                stderr_sample="boom",
                stdout_bytes=0,
                stderr_bytes=0,
                duration_seconds=0.01,
                network_isolation_applied=True,
            )

        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _failing_execute)
        registry = MediaPipelineRegistry()
        registry.register(FakeImageOcrPipeline.get_pipeline_definition(), FakeImageOcrPipeline)
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=2)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="",
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        # Native extraction still succeeds even when OCR fallback cannot render.
        assert result.native_representation is not None
        assert result.unavailable_reason is not None
        assert result.unavailable_reason.error_category == "unrenderable"
        assert result.ocr_outcome is None

    def test_missing_ocr_pipeline_registration_is_unrenderable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "katsi_core.media.document_pipeline.PdfPageRenderPipeline.check_availability",
            lambda self: (True, None),
        )
        monkeypatch.setattr(BoundedSubprocessExecutor, "execute", _fake_render_execute([1]))

        registry = MediaPipelineRegistry()  # No OCR pipeline registered (section 5 not ready yet).
        pipeline = self._pipeline(tmp_path, registry)
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=1)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="",
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        assert result.unavailable_reason is not None
        assert result.unavailable_reason.error_category == "unrenderable"

    def test_renderer_unavailable_without_registry_or_blob_store(self, tmp_path: Path) -> None:
        pipeline = DocumentUnderstandingPipeline(
            thresholds=DocumentTextSufficiencyThresholds(min_chars_per_page=50),
        )
        pdf_path = _write_pdf(tmp_path, "doc.pdf", page_count=2)
        working_directory = tmp_path / "work"
        working_directory.mkdir()

        result = pipeline.process(
            pdf_path,
            resource_version_id=uuid4(),
            source_content_hash=CONTENT_HASH,
            extracted_text="",
            sampling_fingerprint="v1",
            working_directory=working_directory,
        )

        assert result.native_representation is not None
        assert result.unavailable_reason is not None
