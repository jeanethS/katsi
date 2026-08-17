"""Tests for image and screenshot understanding (openspec section 5).

Covers deterministic metadata extraction (dimensions, orientation, color/
alpha, classified fields, EXIF-location privacy gating) and the four
independent image pipelines (thumbnail, OCR, caption, visual embedding),
using tiny synthetic PNG/JPEG/GIF/BMP fixtures built in-test rather than
external files, per design.md Decision 15.
"""

from __future__ import annotations

import json
import struct
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import blake3
import pytest

from katsi_core.media.blob_store import BlobStore
from katsi_core.media.contracts import (
    DerivedRepresentation,
    ImageRegionLocator,
    MediaPrivacyClass,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    WholeResourceLocator,
)
from katsi_core.media.execution import PipelineExecutionOrchestrator
from katsi_core.media.image_metadata import (
    build_image_metadata_representation,
    extract_image_metadata,
)
from katsi_core.media.image_pipeline import (
    ImageCaptionPipeline,
    ImageOcrPipeline,
    ImageThumbnailPipeline,
    ImageVisualEmbeddingPipeline,
    _is_strict_caption,
    build_caption_pipeline_definition,
    build_embedding_pipeline_definition,
    build_ocr_pipeline_definition,
    build_thumbnail_pipeline_definition,
)

# ---------------------------------------------------------------------------
# Tiny synthetic fixture builders
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _content_hash(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def _make_png(width: int = 4, height: int = 2, *, color_type: int = 6, bit_depth: int = 8) -> bytes:
    """Minimal PNG: signature + IHDR chunk only (no pixel data needed by our parsers)."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_body = struct.pack(">II", width, height) + bytes([bit_depth, color_type, 0, 0, 0])
    ihdr_len = struct.pack(">I", len(ihdr_body))
    return signature + ihdr_len + b"IHDR" + ihdr_body + b"\x00\x00\x00\x00"


def _make_png_with_trns(width: int = 4, height: int = 2) -> bytes:
    """Palette PNG (color_type=3) carrying a tRNS chunk (palette transparency)."""
    base = _make_png(width, height, color_type=3, bit_depth=8)
    trns_body = b"\xff\x00\x80"
    trns_chunk = struct.pack(">I", len(trns_body)) + b"tRNS" + trns_body + b"\x00\x00\x00\x00"
    return base + trns_chunk


def _make_gif(width: int = 4, height: int = 2, *, with_transparency: bool = False) -> bytes:
    header = b"GIF89a"
    logical_screen = struct.pack("<HH", width, height) + bytes([0x80, 0, 0])  # global color table, depth 1
    body = header + logical_screen
    if with_transparency:
        body += b"\x21\xf9\x04\x01\x00\x00\x00\x00"
    return body


def _make_bmp(width: int = 4, height: int = 2, *, bits_per_pixel: int = 24) -> bytes:
    header = b"BM" + b"\x00" * 12  # 14-byte BITMAPFILEHEADER (size/reserved/reserved/offset)
    dib = bytearray(40)
    struct.pack_into("<i", dib, 4, width)
    struct.pack_into("<i", dib, 8, height)
    struct.pack_into("<H", dib, 12, 1)  # planes
    struct.pack_into("<H", dib, 14, bits_per_pixel)
    return header + bytes(dib)


def _pack_u16(v: int, big_endian: bool = False) -> bytes:
    return struct.pack(">H" if big_endian else "<H", v)


def _pack_u32(v: int, big_endian: bool = False) -> bytes:
    return struct.pack(">I" if big_endian else "<I", v)


def _make_jpeg_with_exif(
    width: int = 8,
    height: int = 4,
    *,
    orientation: int | None = None,
    make: str | None = None,
    gps: tuple[float, float, str, float, float, str] | None = None,
) -> bytes:
    """Minimal JPEG with SOI, an optional hand-built EXIF APP1 segment, SOF0, EOI.

    `gps` is `(lat_deg, lat_min, lat_ref, lon_deg, lon_min, lon_ref)` with
    seconds fixed at 0 for simplicity. No entropy-coded scan data is
    included since neither our dimension/EXIF parsers nor the color-info
    parser read past the first SOF marker.
    """
    app1 = b""
    if orientation is not None or make is not None or gps is not None:
        ifd0_entries: list[tuple[int, int, int, bytes]] = []
        if make is not None:
            raw = make.encode("ascii") + b"\x00"
            if len(raw) > 4:
                raise ValueError("test helper only supports make <= 3 chars")
            ifd0_entries.append((0x010F, 2, len(raw), raw.ljust(4, b"\x00")))
        if orientation is not None:
            ifd0_entries.append((0x0112, 3, 1, _pack_u16(orientation) + b"\x00\x00"))

        gps_pointer_index: int | None = None
        if gps is not None:
            ifd0_entries.append((0x8825, 4, 1, b"\x00\x00\x00\x00"))
            gps_pointer_index = len(ifd0_entries) - 1

        ifd0_offset = 8
        ifd0_size = 2 + 12 * len(ifd0_entries) + 4
        gps_ifd_offset = ifd0_offset + ifd0_size

        gps_bytes = b""
        if gps is not None:
            lat_deg, lat_min, lat_ref, lon_deg, lon_min, lon_ref = gps
            gps_entries: list[tuple[int, int, int, bytes]] = [
                (1, 2, 2, (lat_ref.encode("ascii") + b"\x00").ljust(4, b"\x00")),
                (3, 2, 2, (lon_ref.encode("ascii") + b"\x00").ljust(4, b"\x00")),
            ]
            gps_ifd_size = 2 + 12 * 4 + 4
            lat_rational_offset = gps_ifd_offset + gps_ifd_size
            lon_rational_offset = lat_rational_offset + 24
            gps_entries.append((2, 5, 3, _pack_u32(lat_rational_offset)))
            gps_entries.append((4, 5, 3, _pack_u32(lon_rational_offset)))
            gps_entries.sort(key=lambda e: e[0])

            gps_bytes = _pack_u16(len(gps_entries))
            for tag, type_, count, val in gps_entries:
                gps_bytes += _pack_u16(tag) + _pack_u16(type_) + _pack_u32(count) + val
            gps_bytes += b"\x00\x00\x00\x00"

            def _rational_triplet(deg: float, minutes: float) -> bytes:
                return (
                    struct.pack("<II", int(deg * 1000), 1000)
                    + struct.pack("<II", int(minutes * 1000), 1000)
                    + struct.pack("<II", 0, 1000)
                )

            gps_bytes += _rational_triplet(lat_deg, lat_min)
            gps_bytes += _rational_triplet(lon_deg, lon_min)

            tag, type_, count, _ = ifd0_entries[gps_pointer_index]  # type: ignore[index]
            ifd0_entries[gps_pointer_index] = (tag, type_, count, _pack_u32(gps_ifd_offset))  # type: ignore[index]

        ifd0_bytes = _pack_u16(len(ifd0_entries))
        for tag, type_, count, val in ifd0_entries:
            ifd0_bytes += _pack_u16(tag) + _pack_u16(type_) + _pack_u32(count) + val
        ifd0_bytes += b"\x00\x00\x00\x00"

        tiff = b"II" + _pack_u16(42) + _pack_u32(8) + ifd0_bytes + gps_bytes
        exif_payload = b"Exif\x00\x00" + tiff
        app1 = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload

    sof_payload = bytes([8]) + struct.pack(">HH", height, width) + bytes([1, 1, 0x11, 0])
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return b"\xff\xd8" + app1 + sof + b"\xff\xd9"


def _escape_braces(script: str) -> str:
    """Escape literal `{`/`}` in a subprocess script before it goes into
    `fixed_args`, since `execution._substitute_args` runs every element
    through `str.format` looking for the placeholder tokens.
    """
    return script.replace("{", "{{").replace("}", "}}")


def _fingerprint(kind: MediaRepresentationKind, stage: PipelineStage) -> PipelineFingerprint:
    return PipelineFingerprint(
        source_content_hash="a" * 32,
        representation_kind=kind,
        stage=stage,
        adapter_name="test",
        adapter_version="1.0.0",
        sampling_fingerprint="test-v1",
    )


# ---------------------------------------------------------------------------
# Task 5.1: deterministic image metadata extraction
# ---------------------------------------------------------------------------


class TestImageMetadataExtraction:
    def test_png_dimensions_color_mode_and_alpha(self, tmp_path):
        data = _make_png(16, 8, color_type=6)  # RGBA
        path = _write(tmp_path, "shot.bin", data)

        result = extract_image_metadata(path, "image/png")

        assert result.width == 16
        assert result.height == 8
        assert result.color_mode == "rgba"
        assert result.has_alpha is True
        assert result.bit_depth == 8
        assert result.malformed is False
        assert result.orientation == 1  # PNG has no EXIF orientation

    def test_png_rgb_has_no_alpha(self, tmp_path):
        data = _make_png(4, 4, color_type=2)  # truecolor, no alpha
        path = _write(tmp_path, "rgb.png", data)

        result = extract_image_metadata(path, "image/png")

        assert result.color_mode == "rgb"
        assert result.has_alpha is False

    def test_transparent_palette_png_detected_via_trns(self, tmp_path):
        data = _make_png_with_trns(4, 4)
        path = _write(tmp_path, "palette.png", data)

        result = extract_image_metadata(path, "image/png")

        assert result.color_mode == "palette"
        assert result.has_alpha is True

    def test_truncated_png_reports_malformed(self, tmp_path):
        path = _write(tmp_path, "broken.png", b"\x89PNG\r\n\x1a\n\x00\x00")

        result = extract_image_metadata(path, "image/png")

        assert result.malformed is True

    def test_gif_transparency_flag_detected(self, tmp_path):
        data = _make_gif(4, 4, with_transparency=True)
        path = _write(tmp_path, "anim.gif", data)

        result = extract_image_metadata(path, "image/gif")

        assert result.width == 4
        assert result.height == 4
        assert result.has_alpha is True

    def test_gif_without_transparency(self, tmp_path):
        data = _make_gif(4, 4, with_transparency=False)
        path = _write(tmp_path, "plain.gif", data)

        result = extract_image_metadata(path, "image/gif")

        assert result.has_alpha is False

    def test_bmp_32bpp_reports_alpha(self, tmp_path):
        data = _make_bmp(4, 4, bits_per_pixel=32)
        path = _write(tmp_path, "img.bmp", data)

        result = extract_image_metadata(path, "image/bmp")

        assert result.width == 4
        assert result.height == 4
        assert result.has_alpha is True
        assert result.color_mode == "rgba"

    def test_jpeg_dimensions_and_default_orientation(self, tmp_path):
        data = _make_jpeg_with_exif(32, 16)
        path = _write(tmp_path, "photo.jpg", data)

        result = extract_image_metadata(path, "image/jpeg")

        assert result.width == 32
        assert result.height == 16
        assert result.orientation == 1
        assert result.privacy_class == MediaPrivacyClass.NONE

    def test_jpeg_rotated_orientation_is_read_from_exif(self, tmp_path):
        data = _make_jpeg_with_exif(32, 16, orientation=6)
        path = _write(tmp_path, "rotated.jpg", data)

        result = extract_image_metadata(path, "image/jpeg")

        assert result.orientation == 6

    def test_jpeg_classified_safe_fields_always_included(self, tmp_path):
        data = _make_jpeg_with_exif(8, 8, make="ABC")
        path = _write(tmp_path, "camera.jpg", data)

        result = extract_image_metadata(path, "image/jpeg", include_privacy_fields=False)

        assert result.safe_fields.get("camera_make") == "ABC"

    def test_jpeg_gps_classified_as_location_privacy(self, tmp_path):
        data = _make_jpeg_with_exif(
            8, 8, gps=(37.0, 46.0, "N", 122.0, 25.0, "W")
        )
        path = _write(tmp_path, "geo.jpg", data)

        result = extract_image_metadata(path, "image/jpeg", include_privacy_fields=False)

        assert result.privacy_class == MediaPrivacyClass.LOCATION
        # Default (no explicit opt-in): privacy fields must never be populated.
        assert result.privacy_fields == {}

    def test_jpeg_gps_fields_only_populated_when_explicitly_requested(self, tmp_path):
        data = _make_jpeg_with_exif(
            8, 8, gps=(37.0, 46.0, "N", 122.0, 25.0, "W")
        )
        path = _write(tmp_path, "geo2.jpg", data)

        result = extract_image_metadata(path, "image/jpeg", include_privacy_fields=True)

        assert result.privacy_class == MediaPrivacyClass.LOCATION
        assert "gps_latitude" in result.privacy_fields
        assert "gps_longitude" in result.privacy_fields
        # sanity: 37 deg 46 min north ~= 37.77
        assert 37.0 < float(result.privacy_fields["gps_latitude"]) < 38.0
        # west longitude must come out negative
        assert float(result.privacy_fields["gps_longitude"]) < 0

    def test_no_gps_means_no_privacy_class(self, tmp_path):
        data = _make_jpeg_with_exif(8, 8, make="ABC")
        path = _write(tmp_path, "nogeo.jpg", data)

        result = extract_image_metadata(path, "image/jpeg")

        assert result.privacy_class == MediaPrivacyClass.NONE


class TestImageMetadataRepresentation:
    def test_builds_current_metadata_representation(self, tmp_path):
        data = _make_png(4, 4, color_type=2)
        path = _write(tmp_path, "a.png", data)

        rep = build_image_metadata_representation(
            path, resource_version_id=uuid4(), source_content_hash=_content_hash(data), mime_type="image/png"
        )

        assert rep.kind == MediaRepresentationKind.METADATA
        assert rep.status == MediaRepresentationStatus.CURRENT
        payload = json.loads(rep.textual_payload)
        assert payload["width"] == 4
        assert payload["height"] == 4
        assert "privacy_fields" not in payload

    def test_gps_privacy_fields_excluded_by_default_from_representation(self, tmp_path):
        data = _make_jpeg_with_exif(8, 8, gps=(37.0, 46.0, "N", 122.0, 25.0, "W"))
        path = _write(tmp_path, "geo.jpg", data)

        rep = build_image_metadata_representation(
            path,
            resource_version_id=uuid4(),
            source_content_hash=_content_hash(data),
            mime_type="image/jpeg",
        )

        payload = json.loads(rep.textual_payload)
        assert payload["privacy_class"] == "location"
        assert "privacy_fields" not in payload

    def test_gps_privacy_fields_included_only_with_explicit_opt_in(self, tmp_path):
        data = _make_jpeg_with_exif(8, 8, gps=(37.0, 46.0, "N", 122.0, 25.0, "W"))
        path = _write(tmp_path, "geo.jpg", data)

        rep = build_image_metadata_representation(
            path,
            resource_version_id=uuid4(),
            source_content_hash=_content_hash(data),
            mime_type="image/jpeg",
            include_privacy_fields=True,
        )

        payload = json.loads(rep.textual_payload)
        assert "privacy_fields" in payload
        assert "gps_latitude" in payload["privacy_fields"]

    def test_unparseable_content_produces_failed_representation(self, tmp_path):
        path = _write(tmp_path, "garbage.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)

        rep = build_image_metadata_representation(
            path,
            resource_version_id=uuid4(),
            source_content_hash=_content_hash(b"garbage"),
            mime_type="image/png",
        )

        assert rep.status == MediaRepresentationStatus.FAILED
        assert rep.error is not None

    def test_missing_file_produces_failed_representation(self, tmp_path):
        path = tmp_path / "does-not-exist.png"

        rep = build_image_metadata_representation(
            path,
            resource_version_id=uuid4(),
            source_content_hash="a" * 32,
            mime_type="image/png",
        )

        assert rep.status == MediaRepresentationStatus.FAILED


# ---------------------------------------------------------------------------
# Task 5.2: orientation-normalized thumbnails
# ---------------------------------------------------------------------------


@pytest.fixture
def blob_store(tmp_path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


class TestImageThumbnailPipeline:
    def _script_writing_png(self, width: int = 4, height: int = 4) -> str:
        png_hex = _make_png(width, height, color_type=6).hex()
        return (
            "import sys; "
            f"data = bytes.fromhex('{png_hex}'); "
            "open(sys.argv[2], 'wb').write(data)"
        )

    def test_process_stores_thumbnail_blob(self, tmp_path, blob_store):
        source = _write(tmp_path, "source.png", _make_png(64, 64))
        definition = build_thumbnail_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", self._script_writing_png(8, 8), "{input_path}", "{output_path}"],
        )
        adapter = ImageThumbnailPipeline(definition=definition, blob_store=blob_store)
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        rep = adapter.process(
            source,
            uuid4(),
            _content_hash(b"source"),
            _fingerprint(MediaRepresentationKind.THUMBNAIL, PipelineStage.GENERATE_THUMBNAIL),
            working_dir,
        )

        assert rep.kind == MediaRepresentationKind.THUMBNAIL
        assert rep.status == MediaRepresentationStatus.CURRENT
        assert rep.blob_hash is not None
        assert blob_store.has_blob(rep.blob_hash)
        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.THUMBNAIL)
        assert is_valid, error

    def test_original_bytes_never_modified(self, tmp_path, blob_store):
        original_bytes = _make_png(64, 64)
        source = _write(tmp_path, "source.png", original_bytes)
        definition = build_thumbnail_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", self._script_writing_png(8, 8), "{input_path}", "{output_path}"],
        )
        adapter = ImageThumbnailPipeline(definition=definition, blob_store=blob_store)
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        adapter.process(
            source,
            uuid4(),
            _content_hash(original_bytes),
            _fingerprint(MediaRepresentationKind.THUMBNAIL, PipelineStage.GENERATE_THUMBNAIL),
            working_dir,
        )

        assert source.read_bytes() == original_bytes

    def test_process_raises_on_nonzero_exit(self, tmp_path, blob_store):
        source = _write(tmp_path, "source.png", _make_png(64, 64))
        definition = build_thumbnail_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import sys; sys.exit(1)"],
        )
        adapter = ImageThumbnailPipeline(definition=definition, blob_store=blob_store)
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        with pytest.raises(RuntimeError):
            adapter.process(
                source,
                uuid4(),
                _content_hash(b"x"),
                _fingerprint(MediaRepresentationKind.THUMBNAIL, PipelineStage.GENERATE_THUMBNAIL),
                working_dir,
            )

    def test_malformed_input_via_orchestrator_yields_failed_representation(self, tmp_path, blob_store):
        """Task 5.7: malformed input never crashes the pipeline; it produces FAILED."""
        source = _write(tmp_path, "garbage.png", b"not an image")
        definition = build_thumbnail_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import sys; sys.exit(2)"],
            timeout_seconds=5.0,
        )
        adapter = ImageThumbnailPipeline(definition=definition, blob_store=blob_store)
        orchestrator = PipelineExecutionOrchestrator()

        rep = orchestrator.run(
            adapter,
            definition,
            source,
            uuid4(),
            _content_hash(b"garbage"),
            _fingerprint(MediaRepresentationKind.THUMBNAIL, PipelineStage.GENERATE_THUMBNAIL),
        )

        assert rep.status == MediaRepresentationStatus.FAILED
        assert rep.error is not None

    def test_validate_output_rejects_missing_blob(self):
        adapter = ImageThumbnailPipeline()
        bad = DerivedRepresentation(
            id=uuid4(),
            resource_version_id=uuid4(),
            kind=MediaRepresentationKind.OCR_TEXT,
            media_type="text/plain",
            status=MediaRepresentationStatus.CURRENT,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            textual_payload="x",
            pipeline_fingerprint=_fingerprint(
                MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name="x",
                adapter_version="1",
            ),
        )
        is_valid, error = adapter.validate_output(bad, MediaRepresentationKind.THUMBNAIL)
        assert is_valid is False
        assert error is not None


# ---------------------------------------------------------------------------
# Task 5.3: whole-image and region-aware OCR
# ---------------------------------------------------------------------------


class _EchoOcrPipeline(ImageOcrPipeline):
    """Test double: overrides the definition to run a controllable python script."""

    _definition: object = None

    @classmethod
    def get_pipeline_definition(cls):
        return cls._definition


def _ocr_definition_writing(payload: dict) -> object:
    script = f"import json,sys; open(sys.argv[2], 'w').write(json.dumps({payload!r}))"
    return build_ocr_pipeline_definition(
        executable_path=sys.executable,
        fixed_args=["-c", _escape_braces(script), "{input_path}", "{output_path}"],
    )


class TestImageOcrPipeline:
    def test_screenshot_whole_image_and_region_text(self, tmp_path):
        source = _write(tmp_path, "screenshot.png", _make_png(100, 100))
        payload = {
            "text": "Save changes?",
            "regions": [
                {"text": "Save", "bbox": [0.1, 0.1, 0.2, 0.1], "confidence": 0.95},
                {"text": "changes?", "bbox": [0.4, 0.1, 0.3, 0.1], "confidence": 0.85},
            ],
        }
        _EchoOcrPipeline._definition = _ocr_definition_writing(payload)
        adapter = _EchoOcrPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        rep = adapter.process(
            source,
            uuid4(),
            _content_hash(b"screenshot"),
            _fingerprint(MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR),
            working_dir,
        )

        assert rep.kind == MediaRepresentationKind.OCR_TEXT
        assert rep.textual_payload == "Save changes?"
        assert rep.confidence == pytest.approx(0.9, abs=0.01)
        region_locators = [loc for loc in rep.locators if isinstance(loc, ImageRegionLocator)]
        assert len(region_locators) == 2
        whole_locators = [loc for loc in rep.locators if isinstance(loc, WholeResourceLocator)]
        assert len(whole_locators) == 1
        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.OCR_TEXT)
        assert is_valid, error

    def test_photograph_without_text_yields_current_empty_ocr(self, tmp_path):
        """Task 5.7: empty OCR must not imply the pipeline failed or the image is unusable."""
        source = _write(tmp_path, "photo.png", _make_png(100, 100))
        _EchoOcrPipeline._definition = _ocr_definition_writing({"text": "", "regions": []})
        adapter = _EchoOcrPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        rep = adapter.process(
            source,
            uuid4(),
            _content_hash(b"photo"),
            _fingerprint(MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR),
            working_dir,
        )

        assert rep.status == MediaRepresentationStatus.CURRENT
        assert rep.textual_payload == ""
        assert rep.coverage.is_complete is True

    def test_malformed_ocr_output_missing_text_key_raises(self, tmp_path):
        source = _write(tmp_path, "screenshot.png", _make_png(10, 10))
        script = "import sys; open(sys.argv[2], 'w').write('{\"regions\": []}')"
        _EchoOcrPipeline._definition = build_ocr_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", _escape_braces(script), "{input_path}", "{output_path}"],
        )
        adapter = _EchoOcrPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        with pytest.raises(RuntimeError):
            adapter.process(
                source,
                uuid4(),
                _content_hash(b"x"),
                _fingerprint(MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR),
                working_dir,
            )

    def test_malformed_input_via_orchestrator_yields_failed_representation(self, tmp_path):
        source = _write(tmp_path, "garbage.png", b"not an image")
        _EchoOcrPipeline._definition = build_ocr_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import sys; sys.exit(3)"],
        )
        adapter = _EchoOcrPipeline()
        orchestrator = PipelineExecutionOrchestrator()

        rep = orchestrator.run(
            adapter,
            _EchoOcrPipeline._definition,
            source,
            uuid4(),
            _content_hash(b"garbage"),
            _fingerprint(MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR),
        )

        assert rep.status == MediaRepresentationStatus.FAILED
        assert rep.textual_payload == ""  # OCR_TEXT contract still satisfied on failure


# ---------------------------------------------------------------------------
# Task 5.4: optional local image captioning
# ---------------------------------------------------------------------------


class _EchoCaptionPipeline(ImageCaptionPipeline):
    _definition: object = None

    @classmethod
    def get_pipeline_definition(cls):
        return cls._definition


def _caption_definition_writing(payload: dict) -> object:
    script = f"import json,sys; open(sys.argv[2], 'w').write(json.dumps({payload!r}))"
    return build_caption_pipeline_definition(
        executable_path=sys.executable,
        fixed_args=["-c", _escape_braces(script), "{input_path}", "{output_path}"],
    )


class TestImageCaptionPipeline:
    def test_process_produces_caption_representation(self, tmp_path):
        source = _write(tmp_path, "photo.png", _make_png(10, 10))
        _EchoCaptionPipeline._definition = _caption_definition_writing(
            {"caption": "A cat sitting on a windowsill.", "confidence": 0.88}
        )
        adapter = _EchoCaptionPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        rep = adapter.process(
            source,
            uuid4(),
            _content_hash(b"photo"),
            _fingerprint(MediaRepresentationKind.IMAGE_CAPTION, PipelineStage.CAPTION),
            working_dir,
        )

        assert rep.kind == MediaRepresentationKind.IMAGE_CAPTION
        assert rep.textual_payload == "A cat sitting on a windowsill."
        assert rep.confidence == pytest.approx(0.88)
        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.IMAGE_CAPTION)
        assert is_valid, error

    def test_empty_caption_rejected_by_strict_contract(self):
        ok, error = _is_strict_caption("   ")
        assert ok is False
        assert error is not None

    def test_overlong_caption_rejected(self):
        ok, error = _is_strict_caption("x" * 5000)
        assert ok is False

    def test_caption_with_control_characters_rejected(self):
        ok, error = _is_strict_caption("hello\x00world")
        assert ok is False

    def test_valid_caption_accepted(self):
        ok, error = _is_strict_caption("A tidy kitchen with a red kettle.")
        assert ok is True
        assert error is None

    def test_process_raises_when_model_output_violates_caption_contract(self, tmp_path):
        source = _write(tmp_path, "photo.png", _make_png(10, 10))
        _EchoCaptionPipeline._definition = _caption_definition_writing({"caption": ""})
        adapter = _EchoCaptionPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        with pytest.raises(RuntimeError):
            adapter.process(
                source,
                uuid4(),
                _content_hash(b"x"),
                _fingerprint(MediaRepresentationKind.IMAGE_CAPTION, PipelineStage.CAPTION),
                working_dir,
            )


# ---------------------------------------------------------------------------
# Task 5.5: optional visual embedding generation
# ---------------------------------------------------------------------------


class _EchoEmbeddingPipeline(ImageVisualEmbeddingPipeline):
    _definition: object = None

    @classmethod
    def get_pipeline_definition(cls):
        return cls._definition


class TestImageVisualEmbeddingPipeline:
    def test_process_produces_embedding_representation(self, tmp_path):
        source = _write(tmp_path, "photo.png", _make_png(10, 10))
        payload = {"embedding": [0.1, 0.2, 0.3, 0.4], "space": "clip_vit_b_32"}
        script = f"import json,sys; open(sys.argv[2], 'w').write(json.dumps({payload!r}))"
        _EchoEmbeddingPipeline._definition = build_embedding_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", _escape_braces(script), "{input_path}", "{output_path}"],
        )
        adapter = _EchoEmbeddingPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        rep = adapter.process(
            source,
            uuid4(),
            _content_hash(b"photo"),
            _fingerprint(MediaRepresentationKind.VISUAL_EMBEDDING, PipelineStage.EMBED_VISUAL),
            working_dir,
        )

        assert rep.kind == MediaRepresentationKind.VISUAL_EMBEDDING
        decoded = json.loads(rep.textual_payload)
        assert decoded["embedding"] == [0.1, 0.2, 0.3, 0.4]
        assert decoded["space"] == "clip_vit_b_32"
        is_valid, error = adapter.validate_output(rep, MediaRepresentationKind.VISUAL_EMBEDDING)
        assert is_valid, error

    def test_empty_embedding_rejected(self, tmp_path):
        source = _write(tmp_path, "photo.png", _make_png(10, 10))
        payload = {"embedding": [], "space": "clip_vit_b_32"}
        script = f"import json,sys; open(sys.argv[2], 'w').write(json.dumps({payload!r}))"
        _EchoEmbeddingPipeline._definition = build_embedding_pipeline_definition(
            executable_path=sys.executable,
            fixed_args=["-c", _escape_braces(script), "{input_path}", "{output_path}"],
        )
        adapter = _EchoEmbeddingPipeline()
        working_dir = tmp_path / "work"
        working_dir.mkdir()

        with pytest.raises(RuntimeError):
            adapter.process(
                source,
                uuid4(),
                _content_hash(b"x"),
                _fingerprint(MediaRepresentationKind.VISUAL_EMBEDDING, PipelineStage.EMBED_VISUAL),
                working_dir,
            )


# ---------------------------------------------------------------------------
# Task 5.6: independence of representation kinds
# ---------------------------------------------------------------------------


class TestRepresentationIndependence:
    def test_each_pipeline_declares_no_dependency_on_sibling_representations(self):
        """Every image pipeline consumes the raw source directly (Decision 5's
        'independent branches' DAG shape), so a missing/failed sibling never
        blocks another representation kind from being produced.
        """
        assert build_thumbnail_pipeline_definition().input_kinds == []
        assert build_ocr_pipeline_definition().input_kinds == []
        assert build_caption_pipeline_definition().input_kinds == []
        assert build_embedding_pipeline_definition().input_kinds == []

    def test_caption_failure_does_not_prevent_ocr_success(self, tmp_path):
        source = _write(tmp_path, "screenshot.png", _make_png(20, 20))

        _EchoCaptionPipeline._definition = _caption_definition_writing({"caption": ""})
        caption_adapter = _EchoCaptionPipeline()
        work_caption = tmp_path / "work-caption"
        work_caption.mkdir()
        with pytest.raises(RuntimeError):
            caption_adapter.process(
                source,
                uuid4(),
                _content_hash(b"x"),
                _fingerprint(MediaRepresentationKind.IMAGE_CAPTION, PipelineStage.CAPTION),
                work_caption,
            )

        _EchoOcrPipeline._definition = _ocr_definition_writing({"text": "hello", "regions": []})
        ocr_adapter = _EchoOcrPipeline()
        work_ocr = tmp_path / "work-ocr"
        work_ocr.mkdir()
        rep = ocr_adapter.process(
            source,
            uuid4(),
            _content_hash(b"x"),
            _fingerprint(MediaRepresentationKind.OCR_TEXT, PipelineStage.OCR),
            work_ocr,
        )
        assert rep.status == MediaRepresentationStatus.CURRENT
        assert rep.textual_payload == "hello"

    def test_metadata_available_even_when_no_other_pipeline_runs(self, tmp_path):
        data = _make_png(4, 4)
        path = _write(tmp_path, "solo.png", data)

        rep = build_image_metadata_representation(
            path,
            resource_version_id=uuid4(),
            source_content_hash=_content_hash(data),
            mime_type="image/png",
        )

        assert rep.status == MediaRepresentationStatus.CURRENT
