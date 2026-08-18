"""Deterministic measurements for multimedia dogfood runs.

These helpers consume observations emitted by the representation registry and
retrieval/context layers.  They deliberately do not execute adapters, so they
are safe to run in CI and complement the real-hardware benchmark harness.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class RepresentationObservation:
    """One request for a representation and the processing run that served it."""

    source_hash: str
    pipeline_fingerprint: str
    representation_kind: str
    processing_run_id: str


@dataclass(frozen=True)
class ReuseMeasurement:
    """Cache reuse across compatible source/pipeline/representation requests."""

    requests: int
    processing_runs: int
    reused_requests: int

    @property
    def reuse_rate(self) -> float:
        return self.reused_requests / self.requests if self.requests else 0.0


@dataclass(frozen=True)
class CoverageMeasurement:
    """Declared completed coverage for one media resource."""

    modality: str
    covered_units: int
    total_units: int

    @property
    def fraction(self) -> float:
        return self.covered_units / self.total_units if self.total_units else 0.0

    @property
    def is_partial(self) -> bool:
        return self.covered_units < self.total_units


@dataclass(frozen=True)
class RetrievalJudgment:
    """Expected and returned representation ids for one modality/query pair."""

    modality: str
    expected_representation_ids: frozenset[str]
    returned_representation_ids: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalQuality:
    """Aggregate retrieval quality for a single modality."""

    modality: str
    queries: int
    recall_at_k: float
    mean_reciprocal_rank: float


@dataclass(frozen=True)
class ScoreCalibrationMeasurement:
    """Score distribution after route-local calibration."""

    embedding_space: str
    scores: tuple[float, ...]

    @property
    def bounded(self) -> bool:
        return all(0.0 <= score <= 1.0 for score in self.scores)


@dataclass(frozen=True)
class ContextCost:
    """Cost of a serialized media context preview, without including binaries."""

    characters: int
    estimated_tokens: int
    binary_bytes: int = 0


def measure_representation_reuse(
    observations: Iterable[RepresentationObservation],
) -> ReuseMeasurement:
    """Measure reuse while treating incompatible fingerprints independently."""
    records = tuple(observations)
    run_ids = {record.processing_run_id for record in records}
    return ReuseMeasurement(
        requests=len(records),
        processing_runs=len(run_ids),
        reused_requests=max(0, len(records) - len(run_ids)),
    )


def measure_coverage(modality: str, covered_units: int, total_units: int) -> CoverageMeasurement:
    """Validate and summarize explicit partial-processing coverage."""
    if total_units < 1:
        raise ValueError("total_units must be positive")
    if not 0 <= covered_units <= total_units:
        raise ValueError("covered_units must be between zero and total_units")
    return CoverageMeasurement(modality, covered_units, total_units)


def measure_retrieval_quality(
    judgments: Iterable[RetrievalJudgment], *, k: int
) -> tuple[RetrievalQuality, ...]:
    """Calculate recall@k and MRR per modality from labeled dogfood queries."""
    if k < 1:
        raise ValueError("k must be positive")
    grouped: dict[str, list[RetrievalJudgment]] = defaultdict(list)
    for judgment in judgments:
        if not judgment.expected_representation_ids:
            raise ValueError("each judgment needs at least one expected representation")
        grouped[judgment.modality].append(judgment)

    results: list[RetrievalQuality] = []
    for modality, entries in sorted(grouped.items()):
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        for entry in entries:
            returned = entry.returned_representation_ids[:k]
            matched = set(returned) & entry.expected_representation_ids
            recalls.append(len(matched) / len(entry.expected_representation_ids))
            rank = next(
                (
                    index
                    for index, value in enumerate(returned, start=1)
                    if value in entry.expected_representation_ids
                ),
                None,
            )
            reciprocal_ranks.append(1.0 / rank if rank is not None else 0.0)
        results.append(
            RetrievalQuality(
                modality=modality,
                queries=len(entries),
                recall_at_k=sum(recalls) / len(recalls),
                mean_reciprocal_rank=sum(reciprocal_ranks) / len(reciprocal_ranks),
            )
        )
    return tuple(results)


def measure_score_calibration(
    embedding_space: str, scores: Iterable[float]
) -> ScoreCalibrationMeasurement:
    """Reject cross-space/unbounded scores before recording calibration evidence."""
    normalized = tuple(scores)
    if not embedding_space:
        raise ValueError("embedding_space is required")
    if any(not 0.0 <= score <= 1.0 for score in normalized):
        raise ValueError("calibrated scores must be in [0, 1]")
    return ScoreCalibrationMeasurement(embedding_space, normalized)


def measure_context_cost(preview: str, *, binary_bytes: int = 0) -> ContextCost:
    """Measure bounded textual context and reject raw media in default bundles."""
    if binary_bytes:
        raise ValueError("default media context must not include binary bytes")
    characters = len(preview)
    return ContextCost(characters=characters, estimated_tokens=(characters + 3) // 4)
