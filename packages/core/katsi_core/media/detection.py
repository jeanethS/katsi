"""Content-signature based media detection.

This module inspects file bytes (magic numbers / container structure) to
determine media type, dimensions, duration, and structural metadata without
executing embedded content. File extensions are treated only as a hint that
may be compared against the detected content type; they are never trusted
on their own.

The detector never invokes external processes and never executes anything
found inside the inspected file. It is pure, deterministic, local inspection
of a bounded byte prefix (and, where needed, small structural scans) of the
target file.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from katsi_core.media.contracts import ContentHash, MediaDescriptor, MediaTypeFamily
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaDetectorProtocol,
    SoftwareDependency,
)

# Bounded prefix read for signature sniffing. Container-specific parsing may
# read additional bounded chunks but never the full file for large media.
_SNIFF_PREFIX_BYTES = 4096
_MAX_STRUCTURAL_SCAN_BYTES = 1_000_000

# Extension -> expected top-level MIME family, used only for mismatch hints.
_EXTENSION_FAMILY_HINTS: dict[str, MediaTypeFamily] = {
    ".png": MediaTypeFamily.IMAGE,
    ".jpg": MediaTypeFamily.IMAGE,
    ".jpeg": MediaTypeFamily.IMAGE,
    ".gif": MediaTypeFamily.IMAGE,
    ".bmp": MediaTypeFamily.IMAGE,
    ".webp": MediaTypeFamily.IMAGE,
    ".tif": MediaTypeFamily.IMAGE,
    ".tiff": MediaTypeFamily.IMAGE,
    ".pdf": MediaTypeFamily.DOCUMENT,
    ".docx": MediaTypeFamily.DOCUMENT,
    ".pptx": MediaTypeFamily.DOCUMENT,
    ".xlsx": MediaTypeFamily.DOCUMENT,
    ".mp3": MediaTypeFamily.AUDIO,
    ".wav": MediaTypeFamily.AUDIO,
    ".flac": MediaTypeFamily.AUDIO,
    ".ogg": MediaTypeFamily.AUDIO,
    ".m4a": MediaTypeFamily.AUDIO,
    ".mp4": MediaTypeFamily.VIDEO,
    ".mov": MediaTypeFamily.VIDEO,
    ".m4v": MediaTypeFamily.VIDEO,
    ".webm": MediaTypeFamily.VIDEO,
    ".mkv": MediaTypeFamily.VIDEO,
    ".avi": MediaTypeFamily.VIDEO,
    ".txt": MediaTypeFamily.TEXT,
    ".md": MediaTypeFamily.TEXT,
}


@dataclass(frozen=True)
class _SignatureMatch:
    mime_type: str
    family: MediaTypeFamily
    container: str | None = None


def _read_prefix(file_path: Path, limit: int = _SNIFF_PREFIX_BYTES) -> bytes:
    with file_path.open("rb") as handle:
        return handle.read(limit)


def _match_signature(prefix: bytes) -> _SignatureMatch | None:
    """Match known file signatures ("magic numbers") against a byte prefix."""
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return _SignatureMatch("image/png", MediaTypeFamily.IMAGE, "png")
    if prefix.startswith(b"\xff\xd8\xff"):
        return _SignatureMatch("image/jpeg", MediaTypeFamily.IMAGE, "jpeg")
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return _SignatureMatch("image/gif", MediaTypeFamily.IMAGE, "gif")
    if prefix.startswith(b"BM"):
        return _SignatureMatch("image/bmp", MediaTypeFamily.IMAGE, "bmp")
    if prefix.startswith(b"RIFF") and len(prefix) >= 12:
        riff_type = prefix[8:12]
        if riff_type == b"WEBP":
            return _SignatureMatch("image/webp", MediaTypeFamily.IMAGE, "webp")
        if riff_type == b"WAVE":
            return _SignatureMatch("audio/wav", MediaTypeFamily.AUDIO, "wav")
        if riff_type == b"AVI ":
            return _SignatureMatch("video/x-msvideo", MediaTypeFamily.VIDEO, "avi")
    if prefix.startswith((b"II*\x00", b"MM\x00*")):
        return _SignatureMatch("image/tiff", MediaTypeFamily.IMAGE, "tiff")
    if prefix.startswith(b"%PDF-"):
        return _SignatureMatch("application/pdf", MediaTypeFamily.DOCUMENT, "pdf")
    if (
        prefix.startswith(b"ID3")
        or prefix.startswith(b"\xff\xfb")
        or prefix.startswith(b"\xff\xf3")
    ):
        return _SignatureMatch("audio/mpeg", MediaTypeFamily.AUDIO, "mp3")
    if prefix.startswith(b"fLaC"):
        return _SignatureMatch("audio/flac", MediaTypeFamily.AUDIO, "flac")
    if prefix.startswith(b"OggS"):
        return _SignatureMatch("audio/ogg", MediaTypeFamily.AUDIO, "ogg")
    if prefix.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML container: Matroska (video/audio) or WebM.
        return _SignatureMatch("video/x-matroska", MediaTypeFamily.VIDEO, "matroska")
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return _match_iso_bmff(prefix)
    if prefix.startswith(b"PK\x03\x04") or prefix.startswith(b"PK\x05\x06"):
        return _SignatureMatch("application/zip", MediaTypeFamily.DOCUMENT, "zip")
    return None


def _match_iso_bmff(prefix: bytes) -> _SignatureMatch:
    """Distinguish MP4/MOV/M4A ISO-BMFF containers by their major brand."""
    major_brand = prefix[8:12]
    audio_brands = {b"M4A ", b"M4B "}
    mov_brands = {b"qt  "}
    heic_brands = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"}
    if major_brand in audio_brands:
        return _SignatureMatch("audio/mp4", MediaTypeFamily.AUDIO, "mp4")
    if major_brand in mov_brands:
        return _SignatureMatch("video/quicktime", MediaTypeFamily.VIDEO, "mov")
    if major_brand in heic_brands:
        return _SignatureMatch("image/heic", MediaTypeFamily.IMAGE, "heic")
    return _SignatureMatch("video/mp4", MediaTypeFamily.VIDEO, "mp4")


def _detect_office_open_xml(file_path: Path) -> tuple[str, MediaTypeFamily] | None:
    """Distinguish DOCX/PPTX/XLSX (zip-based Office Open XML) containers."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return None

    if "word/document.xml" in names:
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            MediaTypeFamily.DOCUMENT,
        )
    if "ppt/presentation.xml" in names:
        return (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            MediaTypeFamily.DOCUMENT,
        )
    if "xl/workbook.xml" in names:
        return (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            MediaTypeFamily.DOCUMENT,
        )
    return None


def _looks_zip_encrypted(file_path: Path) -> bool:
    try:
        with zipfile.ZipFile(file_path) as archive:
            return any(info.flag_bits & 0x1 for info in archive.infolist())
    except (zipfile.BadZipFile, OSError):
        return False


def _looks_pdf_encrypted(file_path: Path) -> bool:
    try:
        with file_path.open("rb") as handle:
            data = handle.read(_MAX_STRUCTURAL_SCAN_BYTES)
    except OSError:
        return False
    return b"/Encrypt" in data


def _png_dimensions(prefix: bytes) -> tuple[int, int] | None:
    if len(prefix) < 24:
        return None
    try:
        width, height = struct.unpack(">II", prefix[16:24])
        return width, height
    except struct.error:
        return None


def _gif_dimensions(prefix: bytes) -> tuple[int, int] | None:
    if len(prefix) < 10:
        return None
    try:
        width, height = struct.unpack("<HH", prefix[6:10])
        return width, height
    except struct.error:
        return None


def _bmp_dimensions(prefix: bytes) -> tuple[int, int] | None:
    if len(prefix) < 26:
        return None
    try:
        width, height = struct.unpack("<ii", prefix[18:26])
        return width, abs(height)
    except struct.error:
        return None


def _jpeg_dimensions(file_path: Path) -> tuple[int, int] | None:
    """Scan JPEG SOF markers for dimensions, bounded to a small read window."""
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    try:
        with file_path.open("rb") as handle:
            data = handle.read(_MAX_STRUCTURAL_SCAN_BYTES)
    except OSError:
        return None

    offset = 2  # skip SOI marker
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
            if offset + 9 > length:
                break
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + segment_length
    return None


class ContentSignatureDetector(MediaDetectorProtocol):
    """Detects media type and structural metadata from file content.

    Uses magic-number signature matching and bounded structural scans. Never
    executes embedded content and never trusts extensions for classification;
    extensions are compared against detected content only to report mismatch.
    """

    _ADAPTER_NAME = "content_signature_detector"
    _ADAPTER_VERSION = "1.0.0"

    @classmethod
    def get_adapter_name(cls) -> str:
        return cls._ADAPTER_NAME

    @classmethod
    def get_adapter_version(cls) -> str:
        return cls._ADAPTER_VERSION

    @classmethod
    def get_supported_mime_patterns(cls) -> list[str]:
        return [
            "image/*",
            "audio/*",
            "video/*",
            "application/pdf",
            "application/zip",
            "application/vnd.*",
        ]

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def detect_media(self, file_path: Path, content_hash: ContentHash) -> MediaDescriptor:
        """Inspect file signature/container structure to build a descriptor.

        Never executes file content. Reads only a bounded prefix and, for
        selected container formats, a bounded structural scan.
        """
        if not file_path.exists():
            return MediaDescriptor(
                mime_type="application/octet-stream",
                extension_hint=file_path.suffix or None,
                family=MediaTypeFamily.UNKNOWN,
                malformed=True,
            )

        prefix = _read_prefix(file_path)
        if not prefix:
            return MediaDescriptor(
                mime_type="application/octet-stream",
                extension_hint=file_path.suffix or None,
                family=MediaTypeFamily.UNKNOWN,
                malformed=True,
            )

        match = _match_signature(prefix)
        extension = file_path.suffix.lower() or None

        if match is None:
            return MediaDescriptor(
                mime_type="application/octet-stream",
                extension_hint=extension,
                family=MediaTypeFamily.UNKNOWN,
                extension_mismatch=False,
                malformed=False,
            )

        mime_type = match.mime_type
        family = match.family
        encrypted = False
        password_protected = False
        malformed = False
        width: int | None = None
        height: int | None = None

        if match.container == "zip":
            office_match = _detect_office_open_xml(file_path)
            if office_match is not None:
                mime_type, family = office_match
            if _looks_zip_encrypted(file_path):
                encrypted = True
                password_protected = True

        if match.container == "pdf" and _looks_pdf_encrypted(file_path):
            encrypted = True
            password_protected = True

        if match.container == "png":
            dims = _png_dimensions(prefix)
            if dims is not None:
                width, height = dims
            else:
                malformed = True
        elif match.container == "gif":
            dims = _gif_dimensions(prefix)
            if dims is not None:
                width, height = dims
            else:
                malformed = True
        elif match.container == "bmp":
            dims = _bmp_dimensions(prefix)
            if dims is not None:
                width, height = dims
            else:
                malformed = True
        elif match.container == "jpeg":
            dims = _jpeg_dimensions(file_path)
            if dims is not None:
                width, height = dims

        extension_mismatch = False
        if extension is not None:
            expected_family = _EXTENSION_FAMILY_HINTS.get(extension)
            if expected_family is not None and expected_family != family:
                extension_mismatch = True

        return MediaDescriptor(
            mime_type=mime_type,
            extension_hint=extension,
            family=family,
            width=width,
            height=height,
            container=match.container,
            extension_mismatch=extension_mismatch,
            encrypted=encrypted,
            password_protected=password_protected,
            malformed=malformed,
        )

    def validate_file_integrity(
        self, file_path: Path, content_hash: ContentHash
    ) -> tuple[bool, str | None]:
        """Validate file bytes match the expected content hash.

        Uses the same hash algorithm family as the workspace blob store
        (blake3 hex digest, matching `ContentHash`'s documented format).
        """
        if not file_path.exists():
            return False, f"File does not exist: {file_path}"

        try:
            import blake3

            digest = blake3.blake3()
            with file_path.open("rb") as handle:
                while chunk := handle.read(1_048_576):
                    digest.update(chunk)
            actual_hash = digest.hexdigest()
        except OSError as e:
            return False, f"Failed to read file for integrity check: {e}"

        if actual_hash != content_hash:
            return False, f"Content hash mismatch: expected {content_hash}, got {actual_hash}"

        return True, None
