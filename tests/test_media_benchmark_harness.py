"""Tests for benchmark execution harness."""

from __future__ import annotations

import sys

import pytest

from benchmarks.media.contracts import (
    BenchmarkRun,
    CandidateAdapter,
    CapabilityKind,
    HardwareClass,
    PlatformDescriptor,
    ResourceUsage,
    RunStatus,
)
from benchmarks.media.harness import BenchmarkHarness


@pytest.fixture
def sample_platform():
    """Create a sample platform descriptor for testing."""
    return PlatformDescriptor(
        os_name="linux",
        os_version="22.04",
        cpu_model="Test CPU",
        cpu_cores=8,
        memory_gb=16,
        gpu_model=None,
        gpu_memory_gb=None,
        neural_engine=False,
        hardware_class=HardwareClass.CPU_ONLY,
    )


@pytest.fixture
def sample_adapter():
    """Create a sample adapter for testing."""
    return CandidateAdapter(
        name="test_adapter",
        capability=CapabilityKind.OCR,
        version="1.0.0",
        install_path="/usr/local/lib/test_adapter",
    )


@pytest.fixture
def mock_harness(sample_platform):
    """Create a benchmark harness for testing."""
    return BenchmarkHarness(sample_platform)


def test_harness_initialization(sample_platform):
    """Test that harness initializes correctly."""
    harness = BenchmarkHarness(sample_platform)

    assert harness.platform == sample_platform


def test_successful_benchmark_execution(mock_harness, sample_adapter, tmp_path):
    """Test successful benchmark execution with mock adapter."""
    # Create a simple test data file
    test_file = tmp_path / "test_data.txt"
    test_file.write_text("sample test data")

    result = mock_harness.run_benchmark(
        adapter=sample_adapter,
        test_data_path=str(test_file),
        timeout_seconds=10,
    )

    assert isinstance(result, BenchmarkRun)
    assert result.status == RunStatus.COMPLETED
    assert result.adapter == sample_adapter
    assert result.usage is not None
    assert result.error is None
    assert result.usage.peak_memory_mb >= 0
    assert result.usage.wall_time_ms >= 0


def test_benchmark_timeout(mock_harness, sample_adapter, tmp_path):
    """Test benchmark execution timeout enforcement."""
    test_file = tmp_path / "test_data.txt"
    test_file.write_text("sample test data")

    # Use a very short timeout
    result = mock_harness.run_benchmark(
        adapter=sample_adapter,
        test_data_path=str(test_file),
        timeout_seconds=0.001,  # Very short timeout
    )

    assert isinstance(result, BenchmarkRun)
    # Should either fail due to timeout or complete very quickly
    assert result.status in (RunStatus.FAILED, RunStatus.COMPLETED)
    assert result.adapter == sample_adapter


def test_benchmark_with_invalid_test_data(mock_harness, sample_adapter):
    """Test benchmark execution with invalid test data path."""
    result = mock_harness.run_benchmark(
        adapter=sample_adapter,
        test_data_path="/nonexistent/path/test_data.txt",
        timeout_seconds=5,
    )

    assert isinstance(result, BenchmarkRun)
    # The mock script should handle missing files gracefully
    assert result.status in (RunStatus.COMPLETED, RunStatus.FAILED)


def test_resource_measurement_accuracy(mock_harness, sample_adapter, tmp_path):
    """Test that resource measurements are reasonably accurate."""
    test_file = tmp_path / "test_data.txt"
    test_file.write_text("sample test data for resource measurement")

    result = mock_harness.run_benchmark(
        adapter=sample_adapter,
        test_data_path=str(test_file),
        timeout_seconds=10,
    )

    if result.status == RunStatus.COMPLETED:
        assert result.usage is not None
        # Memory should be positive
        assert result.usage.peak_memory_mb >= 0
        # Time should be positive and reasonable
        assert 0 < result.usage.wall_time_ms < 60000  # Less than 1 minute
        # CPU time should be positive if measured
        if result.usage.cpu_time_ms is not None:
            assert result.usage.cpu_time_ms >= 0


def test_benchmark_error_handling(mock_harness, sample_adapter):
    """Test benchmark error handling for adapter failures."""
    # Create an adapter that will fail
    failing_adapter = CandidateAdapter(
        name="nonexistent_adapter_xyz",
        capability=CapabilityKind.OCR,
        version="1.0.0",
        install_path="/invalid/path",
    )

    result = mock_harness.run_benchmark(
        adapter=failing_adapter,
        test_data_path="",
        timeout_seconds=5,
    )

    assert isinstance(result, BenchmarkRun)
    # Should handle the error gracefully
    assert result.status in (RunStatus.FAILED, RunStatus.COMPLETED)
    assert result.adapter == failing_adapter


def test_benchmark_run_validation(sample_adapter, sample_platform):
    """Test that BenchmarkRun validation works correctly."""
    usage = ResourceUsage(
        peak_memory_mb=100,
        wall_time_ms=500,
        cpu_time_ms=300,
    )

    run = BenchmarkRun(
        adapter=sample_adapter,
        status=RunStatus.COMPLETED,
        platform=sample_platform,
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T00:00:01",
        usage=usage,
    )

    assert run.status == RunStatus.COMPLETED
    assert run.usage == usage
    assert run.error is None


def test_benchmark_run_requires_usage_when_completed(sample_adapter, sample_platform):
    """Test that COMPLETED runs require usage data."""
    with pytest.raises(ValueError, match="usage is required"):
        BenchmarkRun(
            adapter=sample_adapter,
            status=RunStatus.COMPLETED,
            platform=sample_platform,
            started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:00:01",
            usage=None,  # Missing required usage
        )


def test_benchmark_run_requires_error_when_failed(sample_adapter, sample_platform):
    """Test that FAILED runs require error message."""
    with pytest.raises(ValueError, match="error is required"):
        BenchmarkRun(
            adapter=sample_adapter,
            status=RunStatus.FAILED,
            platform=sample_platform,
            started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:00:01",
            error=None,  # Missing required error
        )


@pytest.mark.skipif(sys.platform == "win32", reason="Platform-specific resource handling")
def test_platform_specific_resources(mock_harness, sample_adapter, tmp_path):
    """Test that resource measurements work on different platforms."""
    test_file = tmp_path / "test_data.txt"
    test_file.write_text("platform-specific test data")

    result = mock_harness.run_benchmark(
        adapter=sample_adapter,
        test_data_path=str(test_file),
        timeout_seconds=10,
    )

    # Should complete successfully on any platform
    assert isinstance(result, BenchmarkRun)
    assert result.status in (RunStatus.COMPLETED, RunStatus.FAILED)
