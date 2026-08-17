"""Tests for content-signature media detection.

Verifies that media type detection relies on file content (magic numbers
and container structure) rather than file extension, correctly reports
extension mismatches, and represents unsupported/encrypted/malformed
content as explicit descriptor states instead of raising or guessing.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import blake3
import pytest

from katsi_core.media.contracts import MediaTypeFamily
from katsi_core.media.detection import ContentSignatureDetector


@pytest.fixture
def detector() -> ContentSignatureDetector:
    return ContentSignatureDetector()


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _content_hash(data: bytes) -> str:
    return blake3.blake3(data).hexdigest()


def _make_png(width: int = 4, height: int = 2) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_body = struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    ihdr_len = struct.pack(">I", len(ihdr_body))
    return signature + ihdr_len + b"IHDR" + ihdr_body + b"\x00\x00\x00\x00"


class TestSignatureDetection:
    def test_detects_png_from_content_ignoring_wrong_extension(self, tmp_path, detector):
        data = _make_png(10, 20)
        path = _write(tmp_path, "photo.txt", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "image/png"
        assert descriptor.family == MediaTypeFamily.IMAGE
        assert descriptor.width == 10
        assert descriptor.height == 20

    def test_reports_extension_mismatch_when_content_disagrees(self, tmp_path, detector):
        data = _make_png()
        path = _write(tmp_path, "not_actually_text.txt", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.extension_mismatch is True

    def test_no_mismatch_when_extension_agrees_with_content(self, tmp_path, detector):
        data = _make_png()
        path = _write(tmp_path, "image.png", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.extension_mismatch is False

    def test_detects_jpeg_signature(self, tmp_path, detector):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        path = _write(tmp_path, "picture.jpg", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "image/jpeg"
        assert descriptor.family == MediaTypeFamily.IMAGE

    def test_detects_pdf_signature(self, tmp_path, detector):
        data = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0 0 obj\n" * 5
        path = _write(tmp_path, "doc.pdf", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "application/pdf"
        assert descriptor.family == MediaTypeFamily.DOCUMENT

    def test_detects_wav_riff_container(self, tmp_path, detector):
        data = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x00" * 20
        path = _write(tmp_path, "sound.wav", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "audio/wav"
        assert descriptor.family == MediaTypeFamily.AUDIO

    def test_detects_mp4_iso_bmff_container(self, tmp_path, detector):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
        path = _write(tmp_path, "clip.mp4", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "video/mp4"
        assert descriptor.family == MediaTypeFamily.VIDEO

    def test_unknown_content_reports_unknown_family_not_exception(self, tmp_path, detector):
        data = b"this is not any known media signature at all"
        path = _write(tmp_path, "mystery.bin", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.family == MediaTypeFamily.UNKNOWN
        assert descriptor.mime_type == "application/octet-stream"

    def test_missing_file_reports_malformed_rather_than_raising(self, tmp_path, detector):
        path = tmp_path / "does_not_exist.png"

        descriptor = detector.detect_media(path, "0" * 32)

        assert descriptor.malformed is True

    def test_empty_file_reports_malformed(self, tmp_path, detector):
        path = _write(tmp_path, "empty.png", b"")

        descriptor = detector.detect_media(path, _content_hash(b""))

        assert descriptor.malformed is True


class TestEncryptedAndMalformedStates:
    def test_encrypted_zip_reports_password_protected(self, tmp_path, detector):
        path = tmp_path / "protected.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("secret.txt", "top secret")

        # Flip the general-purpose bit 0 (encryption flag) in every local
        # file header and central directory record. In-memory ZipInfo edits
        # don't persist through the zipfile module, so patch raw bytes.
        data = bytearray(path.read_bytes())
        offset = 0
        while offset < len(data) - 4:
            if data[offset : offset + 4] == b"PK\x03\x04":
                data[offset + 6] |= 0x1
            elif data[offset : offset + 4] == b"PK\x01\x02":
                data[offset + 8] |= 0x1
            offset += 1
        path.write_bytes(bytes(data))

        data = path.read_bytes()
        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.encrypted is True
        assert descriptor.password_protected is True

    def test_encrypted_pdf_marker_reports_encrypted(self, tmp_path, detector):
        data = b"%PDF-1.4\n" + b"/Encrypt 5 0 R\n" + b"0 0 obj\n" * 5
        path = _write(tmp_path, "locked.pdf", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.encrypted is True
        assert descriptor.password_protected is True

    def test_truncated_png_ihdr_reports_malformed(self, tmp_path, detector):
        # Signature present but too short to contain a valid IHDR chunk.
        data = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
        path = _write(tmp_path, "broken.png", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "image/png"
        assert descriptor.malformed is True

    def test_docx_distinguished_from_plain_zip(self, tmp_path, detector):
        path = tmp_path / "report.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
            archive.writestr("[Content_Types].xml", "<Types/>")

        data = path.read_bytes()
        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )


class TestDeterministicMetadata:
    def test_same_bytes_produce_identical_descriptor(self, tmp_path, detector):
        data = _make_png(7, 3)
        path_a = _write(tmp_path, "a.png", data)
        path_b = _write(tmp_path, "b.png", data)

        descriptor_a = detector.detect_media(path_a, _content_hash(data))
        descriptor_b = detector.detect_media(path_b, _content_hash(data))

        assert descriptor_a == descriptor_b

    def test_never_executes_embedded_content(self, tmp_path, detector, monkeypatch):
        """Detection must never spawn a subprocess for any input."""
        import subprocess

        def _forbidden(*args, **kwargs):
            raise AssertionError("detect_media must never invoke subprocess")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)

        data = _make_png()
        # Content containing shell-like text should still be inert.
        data += b"; rm -rf / #"
        path = _write(tmp_path, "danger.png", data)

        descriptor = detector.detect_media(path, _content_hash(data))

        assert descriptor.mime_type == "image/png"


class TestFileIntegrityValidation:
    def test_validate_file_integrity_accepts_matching_hash(self, tmp_path, detector):
        data = _make_png()
        path = _write(tmp_path, "ok.png", data)

        is_valid, error = detector.validate_file_integrity(path, _content_hash(data))

        assert is_valid is True
        assert error is None

    def test_validate_file_integrity_rejects_mismatched_hash(self, tmp_path, detector):
        data = _make_png()
        path = _write(tmp_path, "ok.png", data)

        is_valid, error = detector.validate_file_integrity(path, "f" * 32)

        assert is_valid is False
        assert error is not None

    def test_validate_file_integrity_rejects_missing_file(self, tmp_path, detector):
        path = tmp_path / "gone.png"

        is_valid, error = detector.validate_file_integrity(path, "f" * 32)

        assert is_valid is False
        assert error is not None


class TestAdapterMetadata:
    def test_adapter_reports_name_and_version(self, detector):
        assert ContentSignatureDetector.get_adapter_name() == "content_signature_detector"
        assert ContentSignatureDetector.get_adapter_version()

    def test_availability_has_no_hardware_or_software_dependency(self):
        assert ContentSignatureDetector.check_availability() == (True, None)
