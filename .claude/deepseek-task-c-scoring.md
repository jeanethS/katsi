# Task C: Accuracy Scoring System

Build the accuracy scoring system that compares adapter outputs against ground truth.

## Context

You are building Task C of a four-part parallel benchmark harness. Four agents are simultaneously building:
- Task A: Probes
- Task B: Harness
- Task C (you): Scoring
- Task D: Report

All tasks share `benchmarks/media/contracts.py` (DO NOT MODIFY).

## Your deliverables

Create these two files:

1. `benchmarks/media/scoring.py` - Accuracy scoring implementations
2. `tests/test_media_benchmark_scoring.py` - Tests

Only touch these files. Never touch another task's files.

## What to build

### scoring.py

Implement accuracy metrics for each media capability:

```python
from contracts import AccuracyScore, AccuracyMetric

# OCR metrics
def character_accuracy(pred_text: str, true_text: str) -> AccuracyScore:
    """Character-level accuracy (edit distance based)."""
    
def word_accuracy(pred_text: str, true_text: str) -> AccuracyScore:
    """Word-level accuracy (exact word matches)."""
    
def text_iou(pred_text: str, true_text: str) -> AccuracyScore:
    """Text IoU for spatial OCR (bounding box overlap)."""

# Transcription/Captioning metrics  
def word_error_rate(pred_text: str, true_text: str) -> AccuracyScore:
    """WER: (substitutions + deletions + insertions) / true_word_count."""
    
def character_error_rate(pred_text: str, true_text: str) -> AccuracyScore:
    """CER: character-level edit distance rate."""
    
def seq2seq_f1(pred_text: str, true_text: str) -> AccuracyScore:
    """F1 score for sequence-to-sequence (token overlap)."""
```

**Implementation rules:**
- NO external dependencies - implement edit distance, tokenization yourself
- Use standard library: `difflib`, `re`, `collections`
- For `text_iou`: assume (x1, y1, x2, y2) format, return intersection/union
- Return `AccuracyScore` with `higher_is_better=True` for accuracy/F1, `False` for error rates
- Handle edge cases: empty strings, unicode, different whitespace
- No hardcoded "good" scores - compute and return only

### tests/test_media_benchmark_scoring.py  

Test each metric:
- Perfect match returns 1.0 (or 0.0 for error rates)
- Completely wrong returns 0.0 (or 1.0 for error rates)
- Known partial matches return expected values
- Test edge cases (empty strings, unicode, whitespace)
- Use fixtures with obvious synthetic values
- Tests deterministic, offline, < 0.2s sleep

## Contracts to respect

Import from `benchmarks.media.contracts`:
- `AccuracyScore`, `AccuracyMetric`, `CapabilityKind`

Reuse from `katsi_core` (DO NOT MODIFY):
- `katsi_core.workspace.contracts`: `StrictModel`, `ImmutableModel`
- `katsi_core.workspace.errors`: `WorkspaceError`

## Technical constraints

- Python 3.12+ with `from __future__ import annotations`
- ruff: line-length 100, rules E,F,I,UP,B,SIM,N, ruff-format style
- NO new third-party dependencies (standard library + pydantic only)
- Implement algorithms yourself: edit distance (Levenshtein), tokenization, IoU calculation
- Never use: numpy, jiwer, Levenshtein packages
- Tests: plain pytest functions, deterministic, offline, use fixtures
- Must pass on both macOS and Linux

## Response format

Return complete files:

FILE: benchmarks/media/scoring.py
```python
# entire file
```

FILE: tests/test_media_benchmark_scoring.py
```python
# entire file
```

NOTES: [any deviations, audit points]
