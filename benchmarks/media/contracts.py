"""Shared contracts for the local media adapter benchmark (OpenSpec task 3.1).

These models are the schema every benchmark component agrees on: probes report
`ProbeResult`, the harness emits `BenchmarkRun`, and the reporter renders a
`BenchmarkReport` into the selected defaults recorded in design/configuration.

Nothing here measures or claims anything by itself. A `BenchmarkRun` is only
valid if it came from an actual execution on real hardware; there is no default
that lets a caller fabricate a score.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class CapabilityKind(StrEnum):
    """Media capability categories."""

    OCR = "ocr"
    TRANSCRIPTION = "transcription"
    CAPTIONING = "captioning"


class HardwareClass(StrEnum):
    """Hardware tiers for benchmark stratification."""

    CPU_ONLY = "cpu_only"
    GPU_ACCEL = "gpu_accel"
    GPU_DEDICATED = "gpu_dedicated"
    NEURAL_ENGINE = "neural_engine"


class RunStatus(StrEnum):
    """Benchmark execution lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AccuracyMetric(StrEnum):
    """Supported accuracy measures by capability."""

    # OCR metrics
    CHARACTER_ACCURACY = "character_accuracy"
    WORD_ACCURACY = "word_accuracy"
    TEXT_IOU = "text_iou"

    # Transcription/Captioning metrics
    WORD_ERROR_RATE = "word_error_rate"
    CHARACTER_ERROR_RATE = "character_error_rate"
    SEQ2SEQ_F1 = "seq2seq_f1"


class PlatformDescriptor(BaseModel):
    """Hardware/platform context for a benchmark run."""

    os_name: str
    os_version: str
    cpu_model: str
    cpu_cores: int
    memory_gb: int | None = None
    gpu_model: str | None = None
    gpu_memory_gb: int | None = None
    neural_engine: bool = False
    hardware_class: HardwareClass


class CandidateAdapter(BaseModel):
    """A media adapter candidate under evaluation."""

    name: str
    capability: CapabilityKind
    version: str
    install_path: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ProbeResult(BaseModel):
    """Result of probing an adapter for availability."""

    available: bool
    adapter: CandidateAdapter
    version_detected: str | None = None
    error_message: str | None = None
    reason: str | None = Field(
        default=None,
        description="Why the adapter is unavailable (required if available=False)",
    )

    @model_validator(mode="after")
    def reason_required_if_unavailable(self) -> ProbeResult:
        if self.available is False and not self.reason:
            raise ValueError("reason is required when available=False")
        return self


class ResourceUsage(BaseModel):
    """Measured resource consumption during a benchmark run."""

    peak_memory_mb: int
    wall_time_ms: int
    cpu_time_ms: int | None = None
    gpu_utilization_percent: float | None = None
    peak_threads: int | None = None


class AccuracyScore(BaseModel):
    """Accuracy result for a single metric."""

    metric: AccuracyMetric
    value: float
    higher_is_better: bool = True

    @field_validator("value")
    @classmethod
    def value_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("accuracy value must be in [0.0, 1.0]")
        return v


class BenchmarkRun(BaseModel):
    """The record of a single benchmark execution."""

    adapter: CandidateAdapter
    status: RunStatus
    platform: PlatformDescriptor
    started_at: str | None = None
    completed_at: str | None = None
    usage: ResourceUsage | None = None
    error: str | None = None
    accuracy_scores: list[AccuracyScore] = Field(default_factory=list)

    @model_validator(mode="after")
    def completed_requires_usage(self) -> BenchmarkRun:
        if self.status == RunStatus.COMPLETED:
            if self.usage is None:
                raise ValueError("usage is required when status=COMPLETED")
            if self.error is not None:
                raise ValueError("error must be None when status=COMPLETED")
        return self

    @model_validator(mode="after")
    def failed_requires_error(self) -> BenchmarkRun:
        if self.status in (RunStatus.FAILED, RunStatus.SKIPPED) and not self.error:
            raise ValueError(f"error is required when status={self.status}")
        return self


class BenchmarkReport(BaseModel):
    """Aggregated benchmark results for a hardware/capability slice."""

    platform: PlatformDescriptor
    capability: CapabilityKind
    runs: list[BenchmarkRun]
    generated_at: str
    best_by_accuracy: str | None = None  # adapter name
    best_by_latency: str | None = None  # adapter name
    best_by_memory: str | None = None  # adapter name
