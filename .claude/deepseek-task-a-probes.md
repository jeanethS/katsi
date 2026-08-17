# Task A: Media Adapter Availability Probes

Build the availability probe system for media adapters (OCR, transcription, captioning).

## Context

You are building Task A of a four-part parallel benchmark harness. Four agents are simultaneously building:
- Task A (you): Probes
- Task B: Harness  
- Task C: Scoring
- Task D: Report

All tasks share `benchmarks/media/contracts.py` (DO NOT MODIFY).

## Your deliverables

Create these two files:

1. `benchmarks/media/probes.py` - Probe implementations
2. `tests/test_media_benchmark_probes.py` - Tests

Only touch these files. Never touch another task's files.

## What to build

### probes.py

Implement availability probes for each media capability:

```python
from contracts import ProbeResult, CandidateAdapter, CapabilityKind

def probe_ocr_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if an OCR adapter is installed and functional."""
    
def probe_transcription_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a transcription adapter is installed and functional."""
    
def probe_captioning_availability(adapter: CandidateAdapter) -> ProbeResult:
    """Check if a captioning adapter is installed and functional."""
```

**Implementation rules:**
- NO network calls, NO imports of media runtimes (torch, PIL, etc.)
- Use `subprocess.run` with `shell=False, capture_output=True, timeout=10` to probe:
  - Try importing the adapter's Python package
  - If import succeeds, try calling a lightweight API (e.g., `model.list_models()`)
  - Return `ProbeResult` with `available=True` if both succeed
  - Return `ProbeResult` with `available=False` and descriptive `reason` if any step fails
- Do NOT hardcode any adapter names, versions, or rankings
- Detect version via `importlib.metadata.version(package_name)` if available
- Each probe must be deterministic: same adapter → same result

### tests/test_media_benchmark_probes.py

Test the probe implementations:
- Test each probe with a known-available adapter (if present on system)
- Test each probe with a known-unavailable adapter  
- Test error handling (malformed adapter, timeout, etc.)
- Use `tmp_path` for any temporary files
- Guard platform-specific tests with `pytest.mark.skipif`
- All tests deterministic, offline, < 0.2s sleep max

## Contracts to respect

Import from `benchmarks.media.contracts`:
- `ProbeResult`, `CandidateAdapter`, `CapabilityKind`

Reuse from `katsi_core` (DO NOT MODIFY):
- `katsi_core.workspace.contracts`: `StrictModel`, `ImmutableModel`
- `katsi_core.workspace.errors`: `WorkspaceError`

## Technical constraints

- Python 3.12+ with `from __future__ import annotations`
- ruff: line-length 100, rules E,F,I,UP,B,SIM,N, ruff-format style
- NO new third-party dependencies (standard library + pydantic only)
- Never import media runtimes (torch, PIL, jiwer, etc.)
- Tests: plain pytest functions, deterministic, offline, use `tmp_path`/`monkeypatch`, < 0.2s sleep
- Must pass on both macOS and Linux

## Response format

Return complete files:

FILE: benchmarks/media/probes.py
```python
# entire file
```

FILE: tests/test_media_benchmark_probes.py  
```python
# entire file
```

NOTES: [any deviations, audit points]
