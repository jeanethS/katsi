"""Pure scoring substrate for retrieval fusion (katsi-scoring-spec.md §3, §4).

Every number that can move a file's rank comes from RetrievalWeights — this
module applies them, it does not define them. No I/O, no store handles: the
functions here take plain data so they stay trivially unit-testable and
deterministic. The score a file gets and the receipt explaining that score are
the same object (`list[Evidence]`); `render_why` derives the legacy one-line
`why` from it.
"""

from __future__ import annotations

from katsi_core.config import RetrievalWeights
from katsi_core.models import Evidence, EvidenceKind, FileHit

# Scores within this distance are treated as tied. Below float noise
# (0.1 + 0.2 != 0.3), above any contribution the weight table can produce.
SCORE_QUANTUM = 1e-6

# How many shared connector names to name in an evidence detail line.
_MAX_NAMED_CONNECTORS = 3


def score_file(evidence: list[Evidence], weights: RetrievalWeights) -> float:
    """Sum contributions, clamp to the configured bounds. The ONLY place a
    file score is produced.
    """
    raw = sum(e.contribution for e in evidence)
    return max(weights.score_min, min(weights.score_max, raw))


def render_why(evidence: list[Evidence]) -> str:
    """Derive the legacy one-line `why` from the receipt: highest-contribution
    details first, joined with ' + '. Empty receipt renders to empty string.
    """
    ordered = sorted(evidence, key=lambda e: e.contribution, reverse=True)
    return " + ".join(e.detail for e in ordered)


# --- evidence builders -------------------------------------------------------
# Each returns Evidence when the signal fired, else None. Callers filter Nones.


def vector_evidence(similarity: float, w: RetrievalWeights) -> Evidence | None:
    if similarity <= 0.0:
        return None
    return Evidence(
        kind=EvidenceKind.VECTOR,
        contribution=w.vector * similarity,
        detail=f"vector similarity {similarity:.2f}",
    )


def _overlap_evidence(
    shared: list[tuple[str, float]],
    kind: EvidenceKind,
    per_shared: float,
    cap: float,
    noun: str,
) -> Evidence | None:
    if not shared:
        return None
    contribution = min(cap, per_shared * sum(weight for _name, weight in shared))
    names = ", ".join(name for name, _ in shared[:_MAX_NAMED_CONNECTORS])
    return Evidence(
        kind=kind,
        contribution=contribution,
        detail=f"shares {len(shared)} {noun}: {names}",
    )


def entity_evidence(shared: list[tuple[str, float]], w: RetrievalWeights) -> Evidence | None:
    """`shared` is [(entity_name, edge_weight), ...]. Contribution scales with
    the SUMMED edge weights (not the count), then caps — so weak edges that
    survive the gate still contribute proportionally less.
    """
    return _overlap_evidence(
        shared, EvidenceKind.ENTITY, w.entity_per_shared, w.entity_cap, "entities"
    )


def topic_evidence(shared: list[tuple[str, float]], w: RetrievalWeights) -> Evidence | None:
    return _overlap_evidence(shared, EvidenceKind.TOPIC, w.topic_per_shared, w.topic_cap, "topics")


def reference_evidence(direction: str, w: RetrievalWeights) -> Evidence | None:
    """`direction` is 'out' (this file links the top hit) or 'in' (the top hit
    links this file). Outbound is the stronger signal — the author wrote it.
    """
    if direction == "out":
        return Evidence(
            kind=EvidenceKind.REFERENCE_OUT,
            contribution=w.reference_out,
            detail="references top hit",
        )
    if direction == "in":
        return Evidence(
            kind=EvidenceKind.REFERENCE_IN,
            contribution=w.reference_in,
            detail="referenced by top hit",
        )
    return None


def duplicate_evidence(similarity: float, w: RetrievalWeights) -> Evidence | None:
    return Evidence(
        kind=EvidenceKind.DUPLICATE,
        contribution=w.duplicate_of,
        detail=f"near-duplicate of top hit ({similarity:.2f})",
    )


def hop_evidence(hops: int, w: RetrievalWeights) -> Evidence | None:
    """Distance decay: penalize each hop beyond the first. First-hop neighbors
    carry no decay, so this returns None for hops <= 1.
    """
    extra = hops - 1
    if extra <= 0:
        return None
    return Evidence(
        kind=EvidenceKind.HOP_DECAY,
        contribution=w.per_extra_hop * extra,
        detail=f"{extra} hop(s) from top hit",
    )


# --- ranking -----------------------------------------------------------------


def _vector_contribution(hit: FileHit) -> float:
    return sum(e.contribution for e in hit.evidence if e.kind is EvidenceKind.VECTOR)


def _rank_key(hit: FileHit) -> tuple[int, int, float, str]:
    return (
        -round(hit.score / SCORE_QUANTUM),  # 1. score, quantized, descending
        -len(hit.evidence),  # 2. more independent signals, descending
        -_vector_contribution(hit),  # 3. vector similarity, descending
        hit.path,  # 4. path ascending — unique, so order is total
    )


def rank_hits(hits: list[FileHit]) -> list[FileHit]:
    """Total, deterministic order over hits. Independent of input order and of
    PYTHONHASHSEED, because the final key (`path`) is unique — no tie reaches
    the sort's stability fallback.
    """
    return sorted(hits, key=_rank_key)
