# Task B: Benchmark Execution Harness

Build the execution harness that runs benchmark workloads and measures resource usage.

## Context

You are building Task B of a four-part parallel benchmark harness. Four agents are simultaneously building:
- Task A: Probes
- Task B (you): Harness
- Task C: Scoring
- Task D: Report

All tasks share `benchmarks/media/contracts.py` (DO NOT MODIFY).

## Your deliverables

Create these two files:

1. `benchmarks/media/harness.py` - Execution harness
2. `tests/test_media_benchmark_harness.py` - Tests

Only touch these files. Never touch another task's files.

## What to build

### harness.py

Implement the benchmark execution harness:

```python
from contracts import BenchmarkRun, CandidateAdapter, PlatformDescriptor, ResourceUsage

class BenchmarkHarness:
    def __init__(self, platform: PlatformDescriptor):
        """Initialize harness for the given hardware platform."""
        
    def run_benchmark(
        self, 
        adapter: CandidateAdapter,
        test_data_path: str,
        timeout_seconds: int = 300
    ) -> BenchmarkRun:
        """Execute a single benchmark run and measure resource usage."""
        
    def _measure_resources(self, process) -> ResourceUsage:
        """Measure peak memory, time, CPU during execution."""
```

**Implementation rules:**
- Use `subprocess.Popen` with `shell=False` to run adapter as subprocess
- Monitor resource usage via `psutil.Process` (standard library only - use `resource` module instead)
- Actually measure: track start time, end time, peak memory (RSS)
- Do NOT fabricate any measurements - real execution only
- Return `BenchmarkRun` with `status=COMPLETED` and real `usage` on success
- Return `BenchmarkRun` with `status=FAILED` and descriptive `error` on failure
- Enforce `timeout_seconds` - kill process if it exceeds
- Capture adapter stdout/stderr for diagnostics

### tests/test_media_benchmark_harness.py

Test the harness:
- Test successful benchmark execution with mock adapter (simple Python script)
- Test timeout enforcement (adapter that exceeds limit)
- Test resource measurement accuracy
- Test error handling (adapter crashes, invalid path, etc.)
- Use `tmp_path` for creating mock adapter scripts
- Tests deterministic, offline, < 0.2s sleep

## Contracts to respect

Import from `benchmarks.media.contracts`:
- `BenchmarkRun`, `CandidateAdapter`, `PlatformDescriptor`, `ResourceUsage`, `RunStatus`

Reuse from `katsi_core` (DO NOT MODIFY):
- `katsi_core.workspace.contracts`: `StrictModel`, `ImmutableModel`
- `katsi_core.workspace.errors`: `WorkspaceError`

## Technical constraints

- Python 3.12+ with `from __future__ import annotations`
- ruff: line-length 100, rules E,F,I,UP,B,SIM,N, ruff-format style  
- NO new third-party dependencies (standard library + pydantic only)
- Never import media runtimes (torch, PIL, jiwer, etc.)
- Use `resource.getrusage()` for memory/CPU measurement (standard library)
- Tests: plain pytest functions, deterministic, offline, use `tmp_path`/`monkeypatch`, < 0.2s sleep
- Must pass on both macOS and Linux (platform-specific resource handling guarded)

## Response format

Return complete files:

FILE: benchmarks/media/harness.py
```python
# entire file
```

FILE: tests/test_media_benchmark_harness.py
```python
# entire file  
```

NOTES: [any deviations, audit points]
