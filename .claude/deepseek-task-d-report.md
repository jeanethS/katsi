# Task D: Benchmark Report Generation

Build the report generation system that aggregates benchmark results and selects winners.

## Context

You are building Task D of a four-part parallel benchmark harness. Four agents are simultaneously building:
- Task A: Probes
- Task B: Harness
- Task C: Scoring
- Task D (you): Report

All tasks share `benchmarks/media/contracts.py` (DO NOT MODIFY).

## Your deliverables

Create these two files:

1. `benchmarks/media/report.py` - Report generation
2. `tests/test_media_benchmark_report.py` - Tests

Only touch these files. Never touch another task's files.

## What to build

### report.py

Implement benchmark report generation:

```python
from contracts import BenchmarkReport, BenchmarkRun

class BenchmarkReporter:
    def __init__(self, platform: PlatformDescriptor, capability: CapabilityKind):
        """Initialize reporter for platform/capability combination."""
        
    def generate_report(
        self, 
        runs: list[BenchmarkRun],
        include_incomplete: bool = False
    ) -> BenchmarkReport:
        """Generate aggregated report from benchmark runs."""
        
    def _select_best_adapter(self, runs: list[BenchmarkRun]) -> dict[str, str]:
        """Select best adapter by accuracy, latency, and memory."""
```

**Implementation rules:**
- Filter to only `COMPLETED` runs unless `include_incomplete=True`
- Compute best per dimension:
  - `best_by_accuracy`: highest average accuracy across all metrics
  - `best_by_latency`: lowest average `wall_time_ms`
  - `best_by_memory`: lowest average `peak_memory_mb`
- Handle ties: first adapter encountered wins (deterministic by sort)
- Include only adapters with at least one successful run
- Return `BenchmarkReport` with populated fields
- Do NOT make recommendations - just aggregate and select

### tests/test_media_benchmark_report.py

Test the reporter:
- Test report generation with mixed run statuses
- Test best adapter selection with clear winner
- Test best adapter selection with ties
- Test empty runs list
- Test with only failed/incomplete runs
- Use fixtures with synthetic `BenchmarkRun` objects
- Tests deterministic, offline, < 0.2s sleep

## Contracts to respect

Import from `benchmarks.media.contracts`:
- `BenchmarkReport`, `BenchmarkRun`, `PlatformDescriptor`, `CapabilityKind`, `RunStatus`, `AccuracyScore`

Reuse from `katsi_core` (DO NOT MODIFY):
- `katsi_core.workspace.contracts`: `StrictModel`, `ImmutableModel`
- `katsi_core.workspace.errors`: `WorkspaceError`

## Technical constraints

- Python 3.12+ with `from __future__ import annotations`
- ruff: line-length 100, rules E,F,I,UP,B,SIM,N, ruff-format style
- NO new third-party dependencies (standard library + pydantic only)
- Use `statistics.mean()` for averages, `min()` for best selection
- Tests: plain pytest functions, deterministic, offline, use fixtures
- Must pass on both macOS and Linux

## Response format

Return complete files:

FILE: benchmarks/media/report.py
```python
# entire file
```

FILE: tests/test_media_benchmark_report.py
```python
# entire file
```

NOTES: [any deviations, audit points]
