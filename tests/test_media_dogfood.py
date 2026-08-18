"""CI-safe measurements for OpenSpec multimedia-understanding task 14.3."""

from __future__ import annotations

import pytest

from benchmarks.media.dogfood import (
    RepresentationObservation,
    RetrievalJudgment,
    measure_context_cost,
    measure_coverage,
    measure_representation_reuse,
    measure_retrieval_quality,
    measure_score_calibration,
)


def test_representation_reuse_counts_only_compatible_cached_requests() -> None:
    observations = [
        RepresentationObservation("same", "ocr-v1", "ocr", "run-1"),
        RepresentationObservation("same", "ocr-v1", "ocr", "run-1"),
        RepresentationObservation("same", "ocr-v2", "ocr", "run-2"),
    ]

    measurement = measure_representation_reuse(observations)

    assert measurement.requests == 3
    assert measurement.processing_runs == 2
    assert measurement.reused_requests == 1
    assert measurement.reuse_rate == pytest.approx(1 / 3)


def test_partial_coverage_is_explicit_and_bounded() -> None:
    coverage = measure_coverage("video", covered_units=7, total_units=10)

    assert coverage.fraction == pytest.approx(0.7)
    assert coverage.is_partial
    with pytest.raises(ValueError, match="between zero"):
        measure_coverage("audio", covered_units=11, total_units=10)


def test_retrieval_quality_is_reported_per_modality() -> None:
    quality = measure_retrieval_quality(
        (
            RetrievalJudgment("ocr", frozenset({"image-1"}), ("image-1", "image-2")),
            RetrievalJudgment("ocr", frozenset({"image-3"}), ("image-2", "image-3")),
            RetrievalJudgment("transcript", frozenset({"audio-1"}), ("audio-2",)),
        ),
        k=2,
    )

    assert quality[0].modality == "ocr"
    assert quality[0].recall_at_k == 1.0
    assert quality[0].mean_reciprocal_rank == pytest.approx(0.75)
    assert quality[1].modality == "transcript"
    assert quality[1].recall_at_k == 0.0


def test_calibration_and_context_cost_reject_invalid_default_context() -> None:
    calibration = measure_score_calibration("clip-v1", (0.0, 0.4, 1.0))
    cost = measure_context_cost("caption " * 10)

    assert calibration.bounded
    assert cost.estimated_tokens == (cost.characters + 3) // 4
    with pytest.raises(ValueError, match="binary"):
        measure_context_cost("preview", binary_bytes=1)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        measure_score_calibration("clip-v1", (1.1,))
