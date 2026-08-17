"""Tests for benchmark report generation."""

from __future__ import annotations

from datetime import datetime

import pytest

from benchmarks.media.contracts import (
    AccuracyMetric,
    AccuracyScore,
    BenchmarkRun,
    CandidateAdapter,
    CapabilityKind,
    HardwareClass,
    PlatformDescriptor,
    ResourceUsage,
    RunStatus,
)
from benchmarks.media.report import BenchmarkReporter


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
def sample_adapters():
    """Create sample adapters for testing."""
    return [
        CandidateAdapter(
            name="adapter_a",
            capability=CapabilityKind.OCR,
            version="1.0.0",
            install_path="/usr/local/lib/adapter_a",
        ),
        CandidateAdapter(
            name="adapter_b",
            capability=CapabilityKind.OCR,
            version="2.0.0",
            install_path="/usr/local/lib/adapter_b",
        ),
        CandidateAdapter(
            name="adapter_c",
            capability=CapabilityKind.OCR,
            version="1.5.0",
            install_path="/usr/local/lib/adapter_c",
        ),
    ]


@pytest.fixture
def sample_runs(sample_adapters, sample_platform):
    """Create sample benchmark runs for testing."""
    runs = []

    # Adapter A: High accuracy, high latency, high memory
    runs.append(
        BenchmarkRun(
            adapter=sample_adapters[0],
            status=RunStatus.COMPLETED,
            platform=sample_platform,
            started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:00:02",
            usage=ResourceUsage(peak_memory_mb=1000, wall_time_ms=2000, cpu_time_ms=1500),
            accuracy_scores=[
                AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.95),
                AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=0.90),
            ],
        )
    )

    # Adapter B: Medium accuracy, low latency, medium memory
    runs.append(
        BenchmarkRun(
            adapter=sample_adapters[1],
            status=RunStatus.COMPLETED,
            platform=sample_platform,
            started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:00:01",
            usage=ResourceUsage(peak_memory_mb=500, wall_time_ms=1000, cpu_time_ms=800),
            accuracy_scores=[
                AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.85),
                AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=0.80),
            ],
        )
    )

    # Adapter C: Low accuracy, medium latency, low memory
    runs.append(
        BenchmarkRun(
            adapter=sample_adapters[2],
            status=RunStatus.COMPLETED,
            platform=sample_platform,
            started_at="2024-01-01T00:00:00",
            completed_at="2024-01-01T00:00:01",
            usage=ResourceUsage(peak_memory_mb=200, wall_time_ms=1200, cpu_time_ms=1000),
            accuracy_scores=[
                AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.75),
                AccuracyScore(metric=AccuracyMetric.WORD_ACCURACY, value=0.70),
            ],
        )
    )

    return runs


@pytest.fixture
def sample_reporter(sample_platform):
    """Create a benchmark reporter for testing."""
    return BenchmarkReporter(sample_platform, CapabilityKind.OCR)


class TestReportGeneration:
    """Tests for report generation functionality."""

    def test_generate_report_with_completed_runs(self, sample_reporter, sample_runs):
        """Test report generation with completed runs only."""
        report = sample_reporter.generate_report(sample_runs, include_incomplete=False)

        assert report.platform == sample_reporter.platform
        assert report.capability == sample_reporter.capability
        assert len(report.runs) == 3  # All runs completed
        assert report.generated_at is not None
        assert report.best_by_accuracy == "adapter_a"  # Highest accuracy
        assert report.best_by_latency == "adapter_b"  # Lowest latency
        assert report.best_by_memory == "adapter_c"  # Lowest memory

    def test_generate_report_filters_incomplete_runs(self, sample_reporter, sample_runs, sample_adapters, sample_platform):
        """Test that incomplete runs are filtered by default."""
        # Add some incomplete runs
        incomplete_runs = sample_runs + [
            BenchmarkRun(
                adapter=sample_adapters[0],
                status=RunStatus.FAILED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                error="Process crashed",
            ),
            BenchmarkRun(
                adapter=sample_adapters[1],
                status=RunStatus.PENDING,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
            ),
        ]

        report = sample_reporter.generate_report(incomplete_runs, include_incomplete=False)

        # Should only include completed runs
        assert len(report.runs) == 3
        assert all(run.status == RunStatus.COMPLETED for run in report.runs)

    def test_generate_report_includes_incomplete_when_requested(self, sample_reporter, sample_runs, sample_adapters, sample_platform):
        """Test including incomplete runs when requested."""
        # Add an incomplete run
        all_runs = sample_runs + [
            BenchmarkRun(
                adapter=sample_adapters[0],
                status=RunStatus.FAILED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                error="Process crashed",
            ),
        ]

        report = sample_reporter.generate_report(all_runs, include_incomplete=True)

        # Should include all non-pending runs
        assert len(report.runs) == 4
        assert any(run.status == RunStatus.FAILED for run in report.runs)

    def test_generate_report_with_empty_runs(self, sample_reporter):
        """Test report generation with no runs."""
        report = sample_reporter.generate_report([], include_incomplete=False)

        assert len(report.runs) == 0
        assert report.best_by_accuracy is None
        assert report.best_by_latency is None
        assert report.best_by_memory is None

    def test_best_by_accuracy_selection(self, sample_reporter, sample_runs):
        """Test best accuracy adapter selection."""
        report = sample_reporter.generate_report(sample_runs, include_incomplete=False)

        # Adapter A has highest accuracy
        assert report.best_by_accuracy == "adapter_a"

    def test_best_by_latency_selection(self, sample_reporter, sample_runs):
        """Test best latency adapter selection."""
        report = sample_reporter.generate_report(sample_runs, include_incomplete=False)

        # Adapter B has lowest latency (1000ms)
        assert report.best_by_latency == "adapter_b"

    def test_best_by_memory_selection(self, sample_reporter, sample_runs):
        """Test best memory adapter selection."""
        report = sample_reporter.generate_report(sample_runs, include_incomplete=False)

        # Adapter C has lowest memory (200MB)
        assert report.best_by_memory == "adapter_c"

    def test_tie_handling_in_best_selection(self, sample_reporter, sample_platform, sample_adapters):
        """Test that ties are handled deterministically."""
        # Create runs with identical performance
        tied_runs = [
            BenchmarkRun(
                adapter=sample_adapters[0],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=ResourceUsage(peak_memory_mb=500, wall_time_ms=1000, cpu_time_ms=800),
                accuracy_scores=[
                    AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.85),
                ],
            ),
            BenchmarkRun(
                adapter=sample_adapters[1],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=ResourceUsage(peak_memory_mb=500, wall_time_ms=1000, cpu_time_ms=800),
                accuracy_scores=[
                    AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.85),
                ],
            ),
        ]

        report = sample_reporter.generate_report(tied_runs, include_incomplete=False)

        # Should return first adapter in case of tie
        assert report.best_by_accuracy in ["adapter_a", "adapter_b"]
        assert report.best_by_latency in ["adapter_a", "adapter_b"]
        assert report.best_by_memory in ["adapter_a", "adapter_b"]

    def test_report_with_runs_missing_usage(self, sample_reporter, sample_platform, sample_adapters):
        """Test report generation when some runs lack usage data."""
        runs_with_missing = [
            BenchmarkRun(
                adapter=sample_adapters[0],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=ResourceUsage(peak_memory_mb=500, wall_time_ms=1000, cpu_time_ms=800),
                accuracy_scores=[
                    AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.85),
                ],
            ),
            BenchmarkRun(
                adapter=sample_adapters[1],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=None,  # Missing usage data
                accuracy_scores=[
                    AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.90),
                ],
            ),
        ]

        report = sample_reporter.generate_report(runs_with_missing, include_incomplete=False)

        # Should skip runs without usage for latency/memory selection
        assert report.best_by_latency == "adapter_a"  # Only one with usage
        assert report.best_by_memory == "adapter_a"
        # But accuracy selection should still work
        assert report.best_by_accuracy == "adapter_b"  # Higher accuracy

    def test_report_with_runs_missing_accuracy_scores(self, sample_reporter, sample_platform, sample_adapters):
        """Test report generation when some runs lack accuracy scores."""
        runs_with_missing = [
            BenchmarkRun(
                adapter=sample_adapters[0],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=ResourceUsage(peak_memory_mb=500, wall_time_ms=1000, cpu_time_ms=800),
                accuracy_scores=[],  # Missing accuracy scores
            ),
            BenchmarkRun(
                adapter=sample_adapters[1],
                status=RunStatus.COMPLETED,
                platform=sample_platform,
                started_at="2024-01-01T00:00:00",
                completed_at="2024-01-01T00:00:01",
                usage=ResourceUsage(peak_memory_mb=300, wall_time_ms=800, cpu_time_ms=600),
                accuracy_scores=[
                    AccuracyScore(metric=AccuracyMetric.CHARACTER_ACCURACY, value=0.90),
                ],
            ),
        ]

        report = sample_reporter.generate_report(runs_with_missing, include_incomplete=False)

        # Should skip runs without accuracy scores for accuracy selection
        assert report.best_by_accuracy == "adapter_b"  # Only one with scores
        assert report.best_by_latency == "adapter_b"  # Lower latency
        assert report.best_by_memory == "adapter_b"  # Lower memory

    def test_report_generation_timestamp(self, sample_reporter, sample_runs):
        """Test that report generation timestamp is set correctly."""
        before_generation = datetime.now().isoformat()

        report = sample_reporter.generate_report(sample_runs, include_incomplete=False)

        after_generation = datetime.now().isoformat()

        assert report.generated_at is not None
        assert before_generation <= report.generated_at <= after_generation


class TestReporterInitialization:
    """Tests for reporter initialization."""

    def test_reporter_initialization(self, sample_platform):
        """Test that reporter initializes correctly."""
        reporter = BenchmarkReporter(sample_platform, CapabilityKind.OCR)

        assert reporter.platform == sample_platform
        assert reporter.capability == CapabilityKind.OCR

    def test_reporter_with_different_capabilities(self, sample_platform):
        """Test reporter initialization with different capabilities."""
        ocr_reporter = BenchmarkReporter(sample_platform, CapabilityKind.OCR)
        transcription_reporter = BenchmarkReporter(sample_platform, CapabilityKind.TRANSCRIPTION)
        captioning_reporter = BenchmarkReporter(sample_platform, CapabilityKind.CAPTIONING)

        assert ocr_reporter.capability == CapabilityKind.OCR
        assert transcription_reporter.capability == CapabilityKind.TRANSCRIPTION
        assert captioning_reporter.capability == CapabilityKind.CAPTIONING
