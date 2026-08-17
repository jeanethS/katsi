"""Tests for media adapter availability probes."""

from __future__ import annotations

import sys
import pytest

from benchmarks.media.contracts import CandidateAdapter, CapabilityKind, ProbeResult
from benchmarks.media.probes import (
    probe_captioning_availability,
    probe_ocr_availability,
    probe_transcription_availability,
)


@pytest.fixture
def sample_ocr_adapter():
    """Create a sample OCR adapter for testing."""
    return CandidateAdapter(
        name="test_ocr_lib",
        capability=CapabilityKind.OCR,
        version="1.0.0",
        install_path="/usr/local/lib/test_ocr",
    )


@pytest.fixture
def sample_transcription_adapter():
    """Create a sample transcription adapter for testing."""
    return CandidateAdapter(
        name="test_transcribe_lib",
        capability=CapabilityKind.TRANSCRIPTION,
        version="2.0.0",
        install_path="/usr/local/lib/test_transcribe",
    )


@pytest.fixture
def sample_captioning_adapter():
    """Create a sample captioning adapter for testing."""
    return CandidateAdapter(
        name="test_caption_lib",
        capability=CapabilityKind.CAPTIONING,
        version="1.5.0",
        install_path="/usr/local/lib/test_caption",
    )


def test_ocr_probe_unavailable_adapter(sample_ocr_adapter):
    """Test OCR probe with a non-existent adapter."""
    result = probe_ocr_availability(sample_ocr_adapter)

    assert isinstance(result, ProbeResult)
    assert result.available is False
    assert result.adapter == sample_ocr_adapter
    assert result.reason is not None
    assert "import failed" in result.reason.lower() or "package" in result.reason.lower()


def test_transcription_probe_unavailable_adapter(sample_transcription_adapter):
    """Test transcription probe with a non-existent adapter."""
    result = probe_transcription_availability(sample_transcription_adapter)

    assert isinstance(result, ProbeResult)
    assert result.available is False
    assert result.adapter == sample_transcription_adapter
    assert result.reason is not None


def test_captioning_probe_unavailable_adapter(sample_captioning_adapter):
    """Test captioning probe with a non-existent adapter."""
    result = probe_captioning_availability(sample_captioning_adapter)

    assert isinstance(result, ProbeResult)
    assert result.available is False
    assert result.adapter == sample_captioning_adapter
    assert result.reason is not None


def test_ocr_probe_with_python_adapter(sample_ocr_adapter):
    """Test OCR probe with Python as a mock adapter."""
    # Use Python itself as a guaranteed-available "adapter"
    python_adapter = CandidateAdapter(
        name="json",  # Standard library module
        capability=CapabilityKind.OCR,
        version="1.0.0",
        install_path="standard_library",
    )

    result = probe_ocr_availability(python_adapter)

    assert isinstance(result, ProbeResult)
    # json module is always available
    assert result.available is True
    assert result.version_detected is not None
    assert result.adapter == python_adapter


def test_transcription_probe_with_python_adapter(sample_transcription_adapter):
    """Test transcription probe with Python as a mock adapter."""
    # Use sys module as a guaranteed-available "adapter"
    sys_adapter = CandidateAdapter(
        name="sys",  # Standard library module
        capability=CapabilityKind.TRANSCRIPTION,
        version="1.0.0",
        install_path="standard_library",
    )

    result = probe_transcription_availability(sys_adapter)

    assert isinstance(result, ProbeResult)
    assert result.available is True
    assert result.version_detected is not None


def test_captioning_probe_with_python_adapter(sample_captioning_adapter):
    """Test captioning probe with Python as a mock adapter."""
    # Use re module as a guaranteed-available "adapter"
    re_adapter = CandidateAdapter(
        name="re",  # Standard library module
        capability=CapabilityKind.CAPTIONING,
        version="1.0.0",
        install_path="standard_library",
    )

    result = probe_captioning_availability(re_adapter)

    assert isinstance(result, ProbeResult)
    assert result.available is True
    assert result.version_detected is not None


def test_probe_result_validation(sample_ocr_adapter):
    """Test that ProbeResult validation works correctly."""
    result = ProbeResult(
        available=False,
        adapter=sample_ocr_adapter,
        reason="Package not found",
    )

    assert result.available is False
    assert result.reason == "Package not found"
    assert result.adapter == sample_ocr_adapter


def test_probe_result_requires_reason_when_unavailable(sample_ocr_adapter):
    """Test that unavailable probes require a reason."""
    with pytest.raises(ValueError, match="reason is required"):
        ProbeResult(
            available=False,
            adapter=sample_ocr_adapter,
            # Missing required reason field
        )


@pytest.mark.skipif(sys.platform == "win32", reason="Platform-specific test")
def test_probes_deterministic(sample_ocr_adapter):
    """Test that probes are deterministic for the same adapter."""
    result1 = probe_ocr_availability(sample_ocr_adapter)
    result2 = probe_ocr_availability(sample_ocr_adapter)

    # Both should give the same result
    assert result1.available == result2.available
    assert result1.adapter == result2.adapter
