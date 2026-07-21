"""Unit tests for the pure scoring substrate (katsi-scoring-spec.md §7).

No I/O, no stores — scoring.py is deliberately pure so these run fast and
pin behavior exactly.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys

from katsi_core.config import RetrievalWeights
from katsi_core.models import Evidence, EvidenceKind, FileHit
from katsi_core.retrieve.scoring import (
    SCORE_QUANTUM,
    duplicate_evidence,
    entity_evidence,
    hop_evidence,
    rank_hits,
    reference_evidence,
    render_why,
    score_file,
    topic_evidence,
    vector_evidence,
)

W = RetrievalWeights()


def _ev(kind: EvidenceKind, contribution: float, detail: str = "d") -> Evidence:
    return Evidence(kind=kind, contribution=contribution, detail=detail)


def _hit(file_id: str, path: str, evidence: list[Evidence]) -> FileHit:
    return FileHit(
        file_id=file_id,
        path=path,
        summary="",
        score=score_file(evidence, W),
        why=render_why(evidence),
        evidence=evidence,
    )


# --- score_file --------------------------------------------------------------


def test_score_is_sum_of_evidence_contributions():
    # Arrange
    evidence = [
        _ev(EvidenceKind.VECTOR, 0.30),
        _ev(EvidenceKind.ENTITY, 0.12),
    ]
    # Act
    score = score_file(evidence, W)
    # Assert
    assert score == 0.42


def test_score_clamps_at_one_when_all_evidence_present():
    evidence = [
        _ev(EvidenceKind.VECTOR, 0.50),
        _ev(EvidenceKind.ENTITY, 0.30),
        _ev(EvidenceKind.TOPIC, 0.20),
        _ev(EvidenceKind.REFERENCE_OUT, 0.25),
        _ev(EvidenceKind.REFERENCE_IN, 0.15),
    ]
    assert score_file(evidence, W) == 1.0


def test_score_clamps_at_zero_when_only_penalties_apply():
    evidence = [_ev(EvidenceKind.DUPLICATE, -0.05), _ev(EvidenceKind.HOP_DECAY, -0.02)]
    assert score_file(evidence, W) == 0.0


def test_score_of_empty_evidence_is_zero():
    assert score_file([], W) == 0.0


# --- evidence builders -------------------------------------------------------


def test_vector_evidence_scales_by_weight():
    ev = vector_evidence(1.0, W)
    assert ev is not None
    assert ev.kind is EvidenceKind.VECTOR
    assert ev.contribution == W.vector


def test_vector_evidence_none_for_zero_similarity():
    assert vector_evidence(0.0, W) is None


def test_entity_evidence_caps_at_entity_cap():
    # 20 shared entities at full edge weight would be 20*0.12 = 2.4 uncapped.
    shared = [(f"e{i}", 1.0) for i in range(20)]
    ev = entity_evidence(shared, W)
    assert ev is not None
    assert ev.contribution == W.entity_cap


def test_entity_evidence_scales_with_edge_weight():
    strong = entity_evidence([("e1", 1.0), ("e2", 1.0)], W)
    weak = entity_evidence([("e1", 0.4), ("e2", 0.4)], W)
    assert strong is not None and weak is not None
    assert strong.contribution > weak.contribution


def test_entity_evidence_none_when_empty():
    assert entity_evidence([], W) is None


def test_entity_evidence_detail_names_shared_entities():
    ev = entity_evidence([("Kùzu", 1.0), ("Leiden", 1.0), ("bge-m3", 1.0)], W)
    assert ev is not None
    assert "Kùzu" in ev.detail


def test_topic_evidence_caps_at_topic_cap():
    shared = [(f"t{i}", 1.0) for i in range(20)]
    ev = topic_evidence(shared, W)
    assert ev is not None
    assert ev.contribution == W.topic_cap


def test_reference_evidence_out_vs_in():
    out = reference_evidence("out", W)
    inn = reference_evidence("in", W)
    assert out is not None and inn is not None
    assert out.kind is EvidenceKind.REFERENCE_OUT
    assert inn.kind is EvidenceKind.REFERENCE_IN
    assert out.contribution == W.reference_out
    assert inn.contribution == W.reference_in


def test_duplicate_evidence_penalizes():
    ev = duplicate_evidence(0.95, W)
    assert ev is not None
    assert ev.contribution < 0
    assert ev.contribution == W.duplicate_of


def test_hop_evidence_none_for_first_hop():
    assert hop_evidence(1, W) is None


def test_hop_evidence_penalizes_extra_hops():
    ev = hop_evidence(3, W)
    assert ev is not None
    assert ev.contribution == W.per_extra_hop * 2


# --- monotonicity ------------------------------------------------------------


def test_adding_evidence_never_lowers_score_for_positive_evidence():
    base = [_ev(EvidenceKind.VECTOR, 0.30)]
    rng = random.Random(0)
    for _ in range(50):
        extra = _ev(EvidenceKind.ENTITY, rng.uniform(0.0, 0.3))
        assert score_file(base + [extra], W) >= score_file(base, W)


# --- render_why --------------------------------------------------------------


def test_render_why_orders_by_contribution_descending():
    evidence = [
        _ev(EvidenceKind.ENTITY, 0.12, "shares entities"),
        _ev(EvidenceKind.VECTOR, 0.50, "vector match"),
    ]
    why = render_why(evidence)
    assert why.index("vector match") < why.index("shares entities")


def test_render_why_nonempty_for_single_evidence():
    assert render_why([_ev(EvidenceKind.VECTOR, 0.5, "vector match")]) == "vector match"


def test_render_why_empty_evidence_is_empty_string():
    assert render_why([]) == ""


# --- rank_hits ---------------------------------------------------------------


def test_rank_hits_orders_by_score_descending():
    hits = [
        _hit("a", "/a", [_ev(EvidenceKind.VECTOR, 0.20)]),
        _hit("b", "/b", [_ev(EvidenceKind.VECTOR, 0.50)]),
    ]
    ranked = rank_hits(hits)
    assert [h.file_id for h in ranked] == ["b", "a"]


def test_ties_break_by_evidence_count():
    # Same score 0.30, different evidence counts.
    more = _hit("more", "/z", [_ev(EvidenceKind.VECTOR, 0.15), _ev(EvidenceKind.ENTITY, 0.15)])
    fewer = _hit("fewer", "/a", [_ev(EvidenceKind.VECTOR, 0.30)])
    ranked = rank_hits([fewer, more])
    assert ranked[0].file_id == "more"


def test_ties_break_by_path_when_all_else_equal():
    h1 = _hit("x", "/z.md", [_ev(EvidenceKind.VECTOR, 0.30)])
    h2 = _hit("y", "/a.md", [_ev(EvidenceKind.VECTOR, 0.30)])
    ranked = rank_hits([h1, h2])
    assert [h.path for h in ranked] == ["/a.md", "/z.md"]


def test_scores_within_quantum_are_treated_as_tied():
    # 0.1 + 0.2 != 0.3 in float. Equal evidence count and zero vector
    # contribution on both, so levels 2 and 3 tie and the path decides —
    # isolating the level-1 quantization from float noise.
    a = _hit("a", "/z", [_ev(EvidenceKind.ENTITY, 0.1 + 0.2)])
    b = _hit("b", "/a", [_ev(EvidenceKind.ENTITY, 0.3)])
    assert abs(a.score - b.score) < SCORE_QUANTUM
    assert a.score != b.score  # genuinely different floats
    ranked = rank_hits([a, b])
    assert ranked[0].path == "/a"  # path tie-break, not float noise


def test_identical_input_shuffled_produces_identical_order():
    hits = [
        _hit(f"f{i}", f"/p{i}", [_ev(EvidenceKind.VECTOR, 0.30)])
        for i in range(10)
    ]
    baseline = [h.file_id for h in rank_hits(hits)]
    rng = random.Random(123)
    for _ in range(20):
        shuffled = hits[:]
        rng.shuffle(shuffled)
        assert [h.file_id for h in rank_hits(shuffled)] == baseline


def test_ranking_is_deterministic_across_hash_seeds():
    """The real §4 defect: set-iteration order varies with PYTHONHASHSEED.
    rank_hits must produce identical output under different seeds. Only a
    subprocess can exercise a different hash seed.
    """
    script = (
        "from katsi_core.config import RetrievalWeights;"
        "from katsi_core.models import Evidence, EvidenceKind, FileHit;"
        "from katsi_core.retrieve.scoring import rank_hits, score_file;"
        "W=RetrievalWeights();"
        "ev=lambda c: Evidence(kind=EvidenceKind.VECTOR, contribution=c, detail='d');"
        "hits=[FileHit(file_id=f'f{i}',path=f'/p{i}',summary='',"
        "score=score_file([ev(0.3)],W),why='',evidence=[ev(0.3)]) for i in range(30)];"
        "print(','.join(h.file_id for h in rank_hits(hits)))"
    )
    def run_with_seed(seed: str) -> str:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        return subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, check=True,
        ).stdout.strip()

    assert run_with_seed("0") == run_with_seed("1")
