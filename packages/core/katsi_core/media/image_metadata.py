"""Deterministic image metadata extraction (dimensions, orientation, EXIF).

This module extends the byte-signature parsing already performed by
`katsi_core.media.detection.ContentSignatureDetector` with the additional
fields the image DAG needs (Decision 6 in design.md):

- pixel dimensions (delegated to the same parsers `detection.py` uses)
- EXIF orientation (JPEG) so downstream thumbnailing can normalize rotation
- color mode / bit depth / alpha presence
- a small set of classified metadata fields, split into a "safe" bucket
  that is always included and a "privacy" bucket (currently: GPS/location)
  that is withheld from the default representation and only attached when
  the caller explicitly opts in via `include_privacy_fields=True` (Decision
  6: "Sensitive EXIF fields are classified before they can enter summaries
  or briefs").

This module never invokes a subprocess and never imports an optional
third-party dependency; it is pure, deterministic, bounded local byte
parsing, matching the same invariants as `detection.py`.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaPrivacyClass,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.detection import (
    _bmp_dimensions,
    _gif_dimensions,
    _jpeg_dimensions,
    _png_dimensions,
    _read_prefix,
)

_ADAPTER_NAME = "image_metadata_extractor"
_ADAPTER_VERSION = "1.0.0"

# EXIF/TIFF tag ids used by this parser. Kept to a small, deliberate set
# rather than a general-purpose EXIF library.
_TAG_ORIENTATION = 0x0112
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_GPS_IFD_POINTER = 0x8825
_TAG_GPS_LAT_REF = 0x0001
_TAG_GPS_LAT = 0x0002
_TAG_GPS_LON_REF = 0x0003
_TAG_GPS_LON = 0x0004

_TYPE_BYTE = 1
_TYPE_ASCII = 2
_TYPE_SHORT = 3
_TYPE_LONG = 4
_TYPE_RATIONAL = 5

_TYPE_SIZES = {_TYPE_BYTE: 1, _TYPE_ASCII: 1, _TYPE_SHORT: 2, _TYPE_LONG: 4, _TYPE_RATIONAL: 8}


@dataclass(frozen=True)
class _IfdEntry:
    tag: int
    type_: int
    count: int
    raw_value: bytes  # the 4-byte value/offset field, as stored


@dataclass(frozen=True)
class ImageMetadataResult:
    """Result of deterministic image inspection.

    `safe_fields` never contains privacy-classified data. `privacy_fields`
    is populated only by `extract_image_metadata(..., include_privacy_fields=True)`
    and callers must gate its use behind an explicit capability grant.
    """

    mime_type: str
    width: int | None
    height: int | None
    bit_depth: int | None
    color_mode: str
    has_alpha: bool
    orientation: int  # EXIF convention: 1 = normal, 1-8
    malformed: bool
    privacy_class: MediaPrivacyClass
    safe_fields: dict[str, str] = field(default_factory=dict)
    privacy_fields: dict[str, str] = field(default_factory=dict)


def _read_u16(data: bytes, offset: int, big_endian: bool) -> int:
    return struct.unpack_from(">H" if big_endian else "<H", data, offset)[0]


def _read_u32(data: bytes, offset: int, big_endian: bool) -> int:
    return struct.unpack_from(">I" if big_endian else "<I", data, offset)[0]


def _parse_ifd(data: bytes, ifd_offset: int, big_endian: bool) -> dict[int, _IfdEntry]:
    """Parse a single TIFF/EXIF IFD into a tag -> entry map.

    Bounded to the bytes already read into `data`; never re-reads the file.
    """
    entries: dict[int, _IfdEntry] = {}
    if ifd_offset + 2 > len(data):
        return entries
    count = _read_u16(data, ifd_offset, big_endian)
    cursor = ifd_offset + 2
    for _ in range(count):
        if cursor + 12 > len(data):
            break
        tag = _read_u16(data, cursor, big_endian)
        type_ = _read_u16(data, cursor + 2, big_endian)
        entry_count = _read_u32(data, cursor + 4, big_endian)
        raw_value = data[cursor + 8 : cursor + 12]
        entries[tag] = _IfdEntry(tag=tag, type_=type_, count=entry_count, raw_value=raw_value)
        cursor += 12
    return entries


def _entry_offset_or_inline(entry: _IfdEntry, big_endian: bool) -> int:
    """Return the byte offset a non-inline entry's value is stored at."""
    return _read_u32(entry.raw_value, 0, big_endian)


def _read_short_entry(entry: _IfdEntry, big_endian: bool) -> int | None:
    if entry.type_ != _TYPE_SHORT:
        return None
    return _read_u16(entry.raw_value, 0, big_endian)


def _read_ascii_entry(
    entry: _IfdEntry, data: bytes, tiff_start: int, big_endian: bool
) -> str | None:
    if entry.type_ != _TYPE_ASCII:
        return None
    size = entry.count
    if size <= 4:
        raw = entry.raw_value[: max(size - 1, 0)]
    else:
        offset = tiff_start + _entry_offset_or_inline(entry, big_endian)
        raw = data[offset : offset + size - 1]
    try:
        return raw.decode("ascii", errors="replace").strip("\x00")
    except Exception:  # noqa: BLE001 -- malformed EXIF must never raise
        return None


def _read_rational_triplet(
    entry: _IfdEntry, data: bytes, tiff_start: int, big_endian: bool
) -> tuple[float, float, float] | None:
    """Read a RATIONAL[3] entry (used for GPS degrees/minutes/seconds)."""
    if entry.type_ != _TYPE_RATIONAL or entry.count < 3:
        return None
    offset = tiff_start + _entry_offset_or_inline(entry, big_endian)
    values: list[float] = []
    for i in range(3):
        base = offset + i * 8
        if base + 8 > len(data):
            return None
        num = _read_u32(data, base, big_endian)
        den = _read_u32(data, base + 4, big_endian)
        values.append(num / den if den else 0.0)
    return values[0], values[1], values[2]


def _parse_jpeg_exif(
    file_path: Path,
) -> tuple[int, dict[str, str], MediaPrivacyClass, dict[str, str]]:
    """Extract EXIF orientation, safe fields, and GPS presence from a JPEG.

    Returns (orientation, safe_fields, privacy_class, privacy_fields).
    Never raises; any parse failure yields defaults (orientation=1, no
    fields, privacy_class=NONE).
    """
    orientation = 1
    safe_fields: dict[str, str] = {}
    privacy_fields: dict[str, str] = {}
    privacy_class = MediaPrivacyClass.NONE

    try:
        with file_path.open("rb") as handle:
            data = handle.read(1_000_000)
    except OSError:
        return orientation, safe_fields, privacy_class, privacy_fields

    offset = 2
    app1_payload: bytes | None = None
    length = len(data)
    while offset < length - 1:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker == 0xDA:  # start of scan: no more markers of interest follow
            break
        if offset + 4 > length:
            break
        segment_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if marker == 0xE1:  # APP1
            payload = data[offset + 4 : offset + 2 + segment_length]
            if payload.startswith(b"Exif\x00\x00"):
                app1_payload = payload[6:]
                break
        offset += 2 + segment_length

    if app1_payload is None or len(app1_payload) < 8:
        return orientation, safe_fields, privacy_class, privacy_fields

    byte_order = app1_payload[0:2]
    if byte_order == b"II":
        big_endian = False
    elif byte_order == b"MM":
        big_endian = True
    else:
        return orientation, safe_fields, privacy_class, privacy_fields

    ifd0_offset = _read_u32(app1_payload, 4, big_endian)
    ifd0 = _parse_ifd(app1_payload, ifd0_offset, big_endian)

    if _TAG_ORIENTATION in ifd0:
        value = _read_short_entry(ifd0[_TAG_ORIENTATION], big_endian)
        if value is not None and 1 <= value <= 8:
            orientation = value

    if _TAG_MAKE in ifd0:
        make = _read_ascii_entry(ifd0[_TAG_MAKE], app1_payload, 0, big_endian)
        if make:
            safe_fields["camera_make"] = make

    if _TAG_MODEL in ifd0:
        model = _read_ascii_entry(ifd0[_TAG_MODEL], app1_payload, 0, big_endian)
        if model:
            safe_fields["camera_model"] = model

    if _TAG_GPS_IFD_POINTER in ifd0:
        gps_offset = _entry_offset_or_inline(ifd0[_TAG_GPS_IFD_POINTER], big_endian)
        gps_ifd = _parse_ifd(app1_payload, gps_offset, big_endian)
        if gps_ifd:
            privacy_class = MediaPrivacyClass.LOCATION
            lat = None
            lon = None
            if _TAG_GPS_LAT in gps_ifd:
                lat = _read_rational_triplet(gps_ifd[_TAG_GPS_LAT], app1_payload, 0, big_endian)
            if _TAG_GPS_LON in gps_ifd:
                lon = _read_rational_triplet(gps_ifd[_TAG_GPS_LON], app1_payload, 0, big_endian)
            lat_ref = (
                _read_ascii_entry(gps_ifd[_TAG_GPS_LAT_REF], app1_payload, 0, big_endian)
                if _TAG_GPS_LAT_REF in gps_ifd
                else None
            )
            lon_ref = (
                _read_ascii_entry(gps_ifd[_TAG_GPS_LON_REF], app1_payload, 0, big_endian)
                if _TAG_GPS_LON_REF in gps_ifd
                else None
            )
            if lat is not None:
                deg, minutes, sec = lat
                decimal = deg + minutes / 60 + sec / 3600
                if lat_ref == "S":
                    decimal = -decimal
                privacy_fields["gps_latitude"] = f"{decimal:.6f}"
            if lon is not None:
                deg, minutes, sec = lon
                decimal = deg + minutes / 60 + sec / 3600
                if lon_ref == "W":
                    decimal = -decimal
                privacy_fields["gps_longitude"] = f"{decimal:.6f}"

    return orientation, safe_fields, privacy_class, privacy_fields


def _png_color_info(prefix: bytes, file_path: Path) -> tuple[int | None, str, bool]:
    """Return (bit_depth, color_mode, has_alpha) for a PNG."""
    if len(prefix) < 26:
        return None, "unknown", False
    bit_depth = prefix[24]
    color_type = prefix[25]
    mode_by_type = {
        0: "grayscale",
        2: "rgb",
        3: "palette",
        4: "grayscale_alpha",
        6: "rgba",
    }
    color_mode = mode_by_type.get(color_type, "unknown")
    has_alpha = color_type in (4, 6)

    if color_type == 3 and not has_alpha:
        # Palette images may carry alpha via a tRNS chunk; scan a bounded
        # region of chunks for its presence without decoding pixel data.
        try:
            with file_path.open("rb") as handle:
                data = handle.read(262_144)
        except OSError:
            data = b""
        if b"tRNS" in data:
            has_alpha = True

    return bit_depth, color_mode, has_alpha


def _jpeg_color_info(file_path: Path) -> tuple[int | None, str]:
    """Return (bit_depth, color_mode) by reading the SOF component count."""
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    try:
        with file_path.open("rb") as handle:
            data = handle.read(1_000_000)
    except OSError:
        return None, "unknown"

    offset = 2
    length = len(data)
    while offset < length - 1:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if offset + 4 > length:
            break
        segment_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if marker in sof_markers:
            if offset + 10 > length:
                break
            precision = data[offset + 4]
            num_components = data[offset + 9]
            mode = {1: "grayscale", 3: "ycbcr", 4: "cmyk"}.get(num_components, "unknown")
            return precision, mode
        offset += 2 + segment_length
    return None, "unknown"


def _gif_color_info(prefix: bytes) -> tuple[int | None, str, bool]:
    if len(prefix) < 11:
        return None, "unknown", False
    packed = prefix[10]
    has_color_table = bool(packed & 0x80)
    bit_depth = ((packed & 0x07) + 1) if has_color_table else None
    # GIF transparency is declared per-frame in a Graphic Control Extension
    # (block introducer 0x21 0xF9); scan a bounded prefix for its presence.
    has_alpha = b"\x21\xf9" in prefix
    return bit_depth, "palette", has_alpha


def _bmp_color_info(prefix: bytes) -> tuple[int | None, str, bool]:
    if len(prefix) < 30:
        return None, "unknown", False
    bits_per_pixel = struct.unpack_from("<H", prefix, 28)[0]
    has_alpha = bits_per_pixel == 32
    mode = "rgba" if has_alpha else ("rgb" if bits_per_pixel >= 24 else "palette")
    return bits_per_pixel, mode, has_alpha


def extract_image_metadata(
    file_path: Path, mime_type: str, *, include_privacy_fields: bool = False
) -> ImageMetadataResult:
    """Deterministically inspect an image file for metadata (Decision 6).

    `mime_type` should come from `ContentSignatureDetector.detect_media`
    (content-sniffed, never trusted from the file extension). Returns a
    best-effort result with `malformed=True` when structural parsing fails;
    never raises for malformed or truncated input.
    """
    if not file_path.exists():
        return ImageMetadataResult(
            mime_type=mime_type,
            width=None,
            height=None,
            bit_depth=None,
            color_mode="unknown",
            has_alpha=False,
            orientation=1,
            malformed=True,
            privacy_class=MediaPrivacyClass.NONE,
        )

    prefix = _read_prefix(file_path)

    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    color_mode = "unknown"
    has_alpha = False
    orientation = 1
    malformed = False
    privacy_class = MediaPrivacyClass.NONE
    safe_fields: dict[str, str] = {}
    privacy_fields: dict[str, str] = {}

    if mime_type == "image/png":
        dims = _png_dimensions(prefix)
        if dims is None:
            malformed = True
        else:
            width, height = dims
        bit_depth, color_mode, has_alpha = _png_color_info(prefix, file_path)
    elif mime_type == "image/jpeg":
        dims = _jpeg_dimensions(file_path)
        if dims is not None:
            width, height = dims
        bit_depth, color_mode = _jpeg_color_info(file_path)
        orientation, safe_fields, privacy_class, jpeg_privacy_fields = _parse_jpeg_exif(file_path)
        if include_privacy_fields:
            privacy_fields = jpeg_privacy_fields
    elif mime_type == "image/gif":
        dims = _gif_dimensions(prefix)
        if dims is None:
            malformed = True
        else:
            width, height = dims
        bit_depth, color_mode, has_alpha = _gif_color_info(prefix)
    elif mime_type == "image/bmp":
        dims = _bmp_dimensions(prefix)
        if dims is None:
            malformed = True
        else:
            width, height = dims
        bit_depth, color_mode, has_alpha = _bmp_color_info(prefix)
    else:
        # Unsupported/unknown image container: report what we can (nothing)
        # without guessing at structure we haven't parsed.
        malformed = not prefix

    if width is not None and width <= 0:
        malformed = True
    if height is not None and height <= 0:
        malformed = True

    return ImageMetadataResult(
        mime_type=mime_type,
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_mode=color_mode,
        has_alpha=has_alpha,
        orientation=orientation,
        malformed=malformed,
        privacy_class=privacy_class,
        safe_fields=safe_fields,
        privacy_fields=privacy_fields if include_privacy_fields else {},
    )


def _fingerprint(
    source_content_hash: ContentHash, sampling_fingerprint: str
) -> PipelineFingerprint:
    return PipelineFingerprint(
        source_content_hash=source_content_hash,
        representation_kind=MediaRepresentationKind.METADATA,
        stage=PipelineStage.EXTRACT_METADATA,
        adapter_name=_ADAPTER_NAME,
        adapter_version=_ADAPTER_VERSION,
        sampling_fingerprint=sampling_fingerprint,
    )


def build_image_metadata_representation(
    file_path: Path,
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    mime_type: str,
    *,
    include_privacy_fields: bool = False,
    sampling_fingerprint: str = "image_metadata_v1",
) -> DerivedRepresentation:
    """Build a METADATA representation from deterministic image inspection.

    `include_privacy_fields` must only be set True by a caller that has
    already checked the workspace's `MediaPrivacyClass.LOCATION` capability
    grant (Decision 6 / `MediaProcessingConfig.require_capability_for_privacy`);
    this function performs the extraction either way but only serializes
    privacy fields into the payload when explicitly asked, so a
    capability-unaware caller can never accidentally leak them by omission
    of a check it forgot to make elsewhere -- the default is always safe.
    """
    now = datetime.now(UTC)
    result = extract_image_metadata(
        file_path, mime_type, include_privacy_fields=include_privacy_fields
    )
    fingerprint = _fingerprint(source_content_hash, sampling_fingerprint)
    rep_id = uuid4()

    if not file_path.exists() or result.width is None or result.height is None:
        # Cannot even establish dimensions: treat as a hard failure rather
        # than fabricating a representation with no useful content.
        return DerivedRepresentation(
            id=rep_id,
            resource_version_id=resource_version_id,
            kind=MediaRepresentationKind.METADATA,
            media_type="application/json",
            status=MediaRepresentationStatus.FAILED,
            created_at=now,
            updated_at=now,
            textual_payload=None,
            coverage=MediaCoverage(
                is_complete=False, coverage_fraction=0.0, detail="Unable to parse image structure"
            ),
            producer=ProducerProvenance(
                producer_type=MediaProducerType.DETERMINISTIC,
                adapter_name=_ADAPTER_NAME,
                adapter_version=_ADAPTER_VERSION,
            ),
            pipeline_fingerprint=fingerprint,
            error=RepresentationError(
                error_category="malformed_input",
                error_message="Could not determine image dimensions from content",
                is_retriable=False,
            ),
        )

    payload: dict[str, object] = {
        "mime_type": result.mime_type,
        "width": result.width,
        "height": result.height,
        "bit_depth": result.bit_depth,
        "color_mode": result.color_mode,
        "has_alpha": result.has_alpha,
        "orientation": result.orientation,
        "malformed": result.malformed,
        "privacy_class": result.privacy_class.value,
        "fields": dict(result.safe_fields),
    }
    if result.privacy_fields:
        payload["privacy_fields"] = dict(result.privacy_fields)

    status = (
        MediaRepresentationStatus.PARTIAL if result.malformed else MediaRepresentationStatus.CURRENT
    )
    coverage = (
        MediaCoverage(is_complete=False, coverage_fraction=0.5, detail="Partial structural parse")
        if result.malformed
        else MediaCoverage(is_complete=True, coverage_fraction=1.0)
    )

    return DerivedRepresentation(
        id=rep_id,
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.METADATA,
        media_type="application/json",
        status=status,
        created_at=now,
        updated_at=now,
        textual_payload=json.dumps(payload, sort_keys=True),
        locators=(
            WholeResourceLocator(resource_version_id=resource_version_id, representation_id=rep_id),
        ),
        coverage=coverage,
        privacy_classes=(
            frozenset({result.privacy_class})
            if result.privacy_class is not MediaPrivacyClass.NONE
            else frozenset()
        ),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
        ),
        pipeline_fingerprint=fingerprint,
    )


# Re-exported so callers don't need to import the detector module just to
# reuse its (private but stable-in-practice) dimension parsers.
__all__ = [
    "ImageMetadataResult",
    "extract_image_metadata",
    "build_image_metadata_representation",
]
