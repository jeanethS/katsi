"""Metrics instrumentation for workspace coordination dogfooding.

Tracks:
- Time to Verified Action
- Brief context generation cost
- Repeated-enrichment avoidance rate
- Reconciliation latency
- Projection lag
- Stale-plan decision detection
- Recovery outcomes
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from katsi_core.workspace.brief import BriefContext


class MetricCategory(StrEnum):
    """Categories of metrics for grouping and analysis."""

    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    CORRECTNESS = "correctness"
    EFFICIENCY = "efficiency"


@dataclass(frozen=True)
class MetricEvent:
    """Immutable metric event with timestamps and metadata."""

    timestamp: datetime
    category: MetricCategory
    name: str
    value: float
    unit: str
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "category": self.category.value,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            **self.metadata,
        }


@dataclass
class MetricSummary:
    """Statistical summary for a metric over a time window."""

    count: int = 0
    total: float = 0.0
    min: float = float("inf")
    max: float = float("-inf")
    last: float | None = None

    @property
    def average(self) -> float:
        """Calculate average value."""
        return self.total / self.count if self.count > 0 else 0.0

    def add(self, value: float) -> None:
        """Add a value to the summary."""
        self.count += 1
        self.total += value
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        self.last = value

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "count": self.count,
            "average": self.average,
            "min": self.min if self.count > 0 else 0.0,
            "max": self.max if self.count > 0 else 0.0,
            "last": self.last,
        }


class WorkspaceMetrics:
    """Thread-safe metrics collection and aggregation for workspace operations.

    Provides:
    - Time to Verified Action tracking
    - Brief context generation cost measurement
    - Repeated-enrichment avoidance monitoring
    - Reconciliation latency measurement
    - Projection lag detection
    - Stale-plan decision detection
    - Recovery outcome tracking
    """

    def __init__(self) -> None:
        """Initialize metrics collector with thread-safe storage."""
        self._lock = Lock()
        self._events: list[MetricEvent] = []
        self._summaries: defaultdict[str, MetricSummary] = defaultdict(
            lambda: MetricSummary(count=0, total=0.0, min=float("inf"), max=float("-inf"))
        )

        # Performance metrics
        self._verified_action_timings: dict[UUID, list[datetime]] = defaultdict(list)
        self._brief_generation_costs: dict[str, list[float]] = defaultdict(list)

        # Efficiency metrics
        self._enrichment_cache_hits: defaultdict[str, int] = defaultdict(int)
        self._enrichment_cache_misses: defaultdict[str, int] = defaultdict(int)

        # Reliability metrics
        self._reconciliation_latencies: list[timedelta] = []
        self._projection_lag_detections: int = 0
        self._stale_plan_blocks: int = 0
        self._recovery_outcomes: defaultdict[str, int] = defaultdict(int)

    @contextmanager
    def track_verified_action(self, agent_id: UUID) -> None:
        """Track time from action start to verification completion.

        Args:
            agent_id: Identity of the agent performing the action

        Yields:
            None
        """
        start_time = datetime.now(UTC)
        action_key = str(agent_id)

        try:
            self._verified_action_timings[action_key].append(start_time)
            yield
        finally:
            duration = datetime.now(UTC) - start_time
            self._record_metric(
                category=MetricCategory.PERFORMANCE,
                name="time_to_verified_action",
                value=duration.total_seconds(),
                unit="seconds",
                metadata={"agent_id": action_key},
            )

    @contextmanager
    def track_brief_generation(self, workspace_id: UUID, token_count: int) -> None:
        """Track computational cost of brief context generation.

        Args:
            workspace_id: Workspace being briefed
            token_count: Number of tokens in generated brief

        Yields:
            None
        """
        start_time = datetime.now(UTC)
        start_memory = self._get_memory_usage()

        try:
            yield
        finally:
            duration = datetime.now(UTC) - start_time
            end_memory = self._get_memory_usage()
            memory_delta = max(0, end_memory - start_memory)

            self._record_metric(
                category=MetricCategory.EFFICIENCY,
                name="brief_generation_duration",
                value=duration.total_seconds(),
                unit="seconds",
                metadata={"workspace_id": str(workspace_id)},
            )

            self._record_metric(
                category=MetricCategory.EFFICIENCY,
                name="brief_generation_memory",
                value=memory_delta,
                unit="bytes",
                metadata={"workspace_id": str(workspace_id)},
            )

            self._brief_generation_costs[str(workspace_id)].append(duration.total_seconds())

    def record_enrichment_cache_hit(self, fingerprint: str) -> None:
        """Record a cache hit for enrichment operation (avoidance).

        Args:
            fingerprint: Cache key that was hit
        """
        with self._lock:
            self._enrichment_cache_hits[fingerprint] += 1

        self._record_metric(
            category=MetricCategory.EFFICIENCY,
            name="enrichment_cache_hit",
            value=1.0,
            unit="count",
            metadata={"fingerprint": fingerprint},
        )

    def record_enrichment_cache_miss(self, fingerprint: str) -> None:
        """Record a cache miss for enrichment operation.

        Args:
            fingerprint: Cache key that was missed
        """
        with self._lock:
            self._enrichment_cache_misses[fingerprint] += 1

        self._record_metric(
            category=MetricCategory.EFFICIENCY,
            name="enrichment_cache_miss",
            value=1.0,
            unit="count",
            metadata={"fingerprint": fingerprint},
        )

    @property
    def enrichment_avoidance_rate(self) -> float:
        """Calculate rate at which enrichment was cached (avoided).

        Returns:
            Rate between 0.0 and 1.0, or 0.0 if no operations
        """
        with self._lock:
            total_hits = sum(self._enrichment_cache_hits.values())
            total_misses = sum(self._enrichment_cache_misses.values())
            total = total_hits + total_misses

            return total_hits / total if total > 0 else 0.0

    @contextmanager
    def track_reconciliation(self, workspace_id: UUID) -> None:
        """Track latency of reconciliation operations.

        Args:
            workspace_id: Workspace being reconciled

        Yields:
            None
        """
        start_time = datetime.now(UTC)

        try:
            yield
        finally:
            duration = datetime.now(UTC) - start_time
            self._reconciliation_latencies.append(duration)

            self._record_metric(
                category=MetricCategory.RELIABILITY,
                name="reconciliation_latency",
                value=duration.total_seconds(),
                unit="seconds",
                metadata={"workspace_id": str(workspace_id)},
            )

    def record_projection_lag(self, workspace_id: UUID, lag_seconds: float) -> None:
        """Record detection of projection lag (staleness between model and reality).

        Args:
            workspace_id: Workspace with projection lag
            lag_seconds: Duration of the detected lag
        """
        with self._lock:
            self._projection_lag_detections += 1

        self._record_metric(
            category=MetricCategory.RELIABILITY,
            name="projection_lag",
            value=lag_seconds,
            unit="seconds",
            metadata={"workspace_id": str(workspace_id)},
        )

    def record_stale_plan_blocked(
        self, workspace_id: UUID, plan_id: UUID, reason: str
    ) -> None:
        """Record blocking of a stale proposal/plan.

        Args:
            workspace_id: Workspace where stale plan was blocked
            plan_id: Identifier of the blocked plan
            reason: Why the plan was considered stale
        """
        with self._lock:
            self._stale_plan_blocks += 1

        self._record_metric(
            category=MetricCategory.CORRECTNESS,
            name="stale_plan_blocked",
            value=1.0,
            unit="count",
            metadata={
                "workspace_id": str(workspace_id),
                "plan_id": str(plan_id),
                "reason": reason,
            },
        )

    def record_recovery_outcome(
        self,
        recovery_type: str,
        success: bool,
        duration_seconds: float,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Record outcome of a recovery operation.

        Args:
            recovery_type: Type of recovery performed (e.g., "reconciliation", "restart")
            success: Whether recovery succeeded
            duration_seconds: How long recovery took
            metadata: Additional context about the recovery
        """
        with self._lock:
            outcome_key = f"{recovery_type}_{'success' if success else 'failure'}"
            self._recovery_outcomes[outcome_key] += 1

        self._record_metric(
            category=MetricCategory.RELIABILITY,
            name="recovery_outcome",
            value=1.0 if success else 0.0,
            unit="success_rate",
            metadata={
                "recovery_type": recovery_type,
                "duration_seconds": str(duration_seconds),
                **(metadata or {}),
            },
        )

    def get_summary(self, metric_name: str) -> MetricSummary:
        """Get statistical summary for a specific metric.

        Args:
            metric_name: Name of the metric to summarize

        Returns:
            Summary statistics for the metric
        """
        with self._lock:
            return self._summaries.get(metric_name, MetricSummary())

    def get_all_summaries(self) -> dict[str, MetricSummary]:
        """Get all metric summaries.

        Returns:
            Dictionary mapping metric names to their summaries
        """
        with self._lock:
            return dict(self._summaries)

    def get_events_since(self, since: datetime) -> list[MetricEvent]:
        """Get all metric events since a given timestamp.

        Args:
            since: Starting timestamp for event retrieval

        Returns:
            List of events that occurred after the timestamp
        """
        with self._lock:
            return [event for event in self._events if event.timestamp >= since]

    def get_all_events(self) -> list[MetricEvent]:
        """Get all recorded metric events.

        Returns:
            All metric events in chronological order
        """
        with self._lock:
            return list(self._events)

    def export_metrics(self) -> dict:
        """Export all metrics as a dictionary for serialization.

        Returns:
            Nested dictionary with all metrics and summaries
        """
        with self._lock:
            return {
                "summaries": {
                    name: summary.to_dict()
                    for name, summary in self._summaries.items()
                },
                "events": [event.to_dict() for event in self._events],
                "aggregates": {
                    "enrichment_avoidance_rate": self.enrichment_avoidance_rate,
                    "projection_lag_detections": self._projection_lag_detections,
                    "stale_plan_blocks": self._stale_plan_blocks,
                    "recovery_outcomes": dict(self._recovery_outcomes),
                },
            }

    def reset(self) -> None:
        """Clear all metrics (useful for testing or isolation)."""
        with self._lock:
            self._events.clear()
            self._summaries.clear()
            self._verified_action_timings.clear()
            self._brief_generation_costs.clear()
            self._enrichment_cache_hits.clear()
            self._enrichment_cache_misses.clear()
            self._reconciliation_latencies.clear()
            self._projection_lag_detections = 0
            self._stale_plan_blocks = 0
            self._recovery_outcomes.clear()

    def _record_metric(
        self,
        category: MetricCategory,
        name: str,
        value: float,
        unit: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        """Record a metric event and update summaries.

        Args:
            category: Category of the metric
            name: Name of the metric
            value: Numerical value
            unit: Unit of measurement
            metadata: Additional context
        """
        event = MetricEvent(
            timestamp=datetime.now(UTC),
            category=category,
            name=name,
            value=value,
            unit=unit,
            metadata=metadata or {},
        )

        with self._lock:
            self._events.append(event)
            self._summaries[name].add(value)

    @staticmethod
    def _get_memory_usage() -> float:
        """Get current process memory usage in bytes.

        Returns:
            Memory usage in bytes
        """
        try:
            import psutil

            return float(psutil.Process().memory_info().rss)
        except ImportError:
            return 0.0


# Global metrics instance for process-wide collection
_global_metrics: WorkspaceMetrics | None = None
_global_metrics_lock = Lock()


def get_global_metrics() -> WorkspaceMetrics:
    """Get or create the global metrics collector.

    Returns:
        Global WorkspaceMetrics instance
    """
    global _global_metrics

    if _global_metrics is None:
        with _global_metrics_lock:
            if _global_metrics is None:
                _global_metrics = WorkspaceMetrics()

    return _global_metrics


def reset_global_metrics() -> None:
    """Reset the global metrics collector (primarily for testing)."""
    global _global_metrics

    with _global_metrics_lock:
        if _global_metrics is not None:
            _global_metrics.reset()
            _global_metrics = None
