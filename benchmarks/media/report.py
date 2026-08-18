"""Benchmark report generation.

Aggregates benchmark results and selects best adapters by different metrics.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from benchmarks.media.contracts import (
    BenchmarkReport,
    BenchmarkRun,
    CapabilityKind,
    PlatformDescriptor,
    RunStatus,
)


class BenchmarkReporter:
    """Generates aggregated reports from benchmark runs."""

    def __init__(self, platform: PlatformDescriptor, capability: CapabilityKind):
        """Initialize reporter for platform/capability combination."""
        self.platform = platform
        self.capability = capability

    def generate_report(
        self,
        runs: list[BenchmarkRun],
        include_incomplete: bool = False,
    ) -> BenchmarkReport:
        """Generate aggregated report from benchmark runs."""
        # Filter runs by status
        if include_incomplete:
            filtered_runs = [r for r in runs if r.status != RunStatus.PENDING]
        else:
            filtered_runs = [r for r in runs if r.status == RunStatus.COMPLETED]

        # Select best adapters
        best_by_accuracy = None
        best_by_latency = None
        best_by_memory = None

        if filtered_runs:
            best_by_accuracy = self._select_best_by_accuracy(filtered_runs)
            best_by_latency = self._select_best_by_latency(filtered_runs)
            best_by_memory = self._select_best_by_memory(filtered_runs)

        return BenchmarkReport(
            platform=self.platform,
            capability=self.capability,
            runs=filtered_runs,
            generated_at=datetime.now().isoformat(),
            best_by_accuracy=best_by_accuracy,
            best_by_latency=best_by_latency,
            best_by_memory=best_by_memory,
        )

    def _select_best_by_accuracy(self, runs: list[BenchmarkRun]) -> str | None:
        """Select best adapter by highest average accuracy."""
        if not runs:
            return None

        # Group runs by adapter
        adapter_scores = {}
        for run in runs:
            adapter_name = run.adapter.name
            if not run.accuracy_scores:
                continue

            # Average all accuracy scores for this run
            avg_score = statistics.mean(
                score.value for score in run.accuracy_scores if score.higher_is_better
            )

            if adapter_name not in adapter_scores:
                adapter_scores[adapter_name] = []
            adapter_scores[adapter_name].append(avg_score)

        if not adapter_scores:
            return None

        # Calculate overall average per adapter
        adapter_averages = {
            name: statistics.mean(scores) for name, scores in adapter_scores.items()
        }

        # Return adapter with highest average accuracy
        best_adapter = max(adapter_averages, key=adapter_averages.get)
        return best_adapter

    def _select_best_by_latency(self, runs: list[BenchmarkRun]) -> str | None:
        """Select best adapter by lowest average wall time."""
        if not runs:
            return None

        # Group runs by adapter
        adapter_times = {}
        for run in runs:
            if not run.usage:
                continue
            adapter_name = run.adapter.name

            if adapter_name not in adapter_times:
                adapter_times[adapter_name] = []
            adapter_times[adapter_name].append(run.usage.wall_time_ms)

        if not adapter_times:
            return None

        # Calculate overall average per adapter
        adapter_averages = {name: statistics.mean(times) for name, times in adapter_times.items()}

        # Return adapter with lowest average latency
        best_adapter = min(adapter_averages, key=adapter_averages.get)
        return best_adapter

    def _select_best_by_memory(self, runs: list[BenchmarkRun]) -> str | None:
        """Select best adapter by lowest average peak memory."""
        if not runs:
            return None

        # Group runs by adapter
        adapter_memory = {}
        for run in runs:
            if not run.usage:
                continue
            adapter_name = run.adapter.name

            if adapter_name not in adapter_memory:
                adapter_memory[adapter_name] = []
            adapter_memory[adapter_name].append(run.usage.peak_memory_mb)

        if not adapter_memory:
            return None

        # Calculate overall average per adapter
        adapter_averages = {name: statistics.mean(mem) for name, mem in adapter_memory.items()}

        # Return adapter with lowest average memory
        best_adapter = min(adapter_averages, key=adapter_averages.get)
        return best_adapter
