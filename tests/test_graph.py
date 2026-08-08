"""Tests for GraphStore (Kùzu-backed)."""

from __future__ import annotations

from katsi_core.models import FileRecord, IndexStatus
from katsi_core.store.graph import GraphStore


def test_schema_init_idempotent(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    gs.init_schema()  # second call must not raise


def test_upsert_and_get_file(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f = FileRecord(
        id="abc",
        path="/x/y.md",
        name="y.md",
        ext=".md",
        mime="text/markdown",
        size_bytes=100,
        mtime=1234.5,
        content_hash="h1",
        status=IndexStatus.INDEXED,
        summary="some summary",
    )
    gs.upsert_file(f)
    got = gs.get_file("abc")
    assert got is not None
    assert got.id == "abc"
    assert got.path == "/x/y.md"
    assert got.summary == "some summary"
    assert got.mtime == 1234.5


def test_get_missing_file_returns_none(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    assert gs.get_file("nope") is None


def test_count_nodes(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    gs.upsert_entity("Acme", "organization")
    gs.upsert_topic("AI")

    assert gs.count_nodes() == {"entities": 1, "topics": 1}


def test_mentions_and_peers(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    gs.add_mentions("f1", [{"name": "Acme", "kind": "org"}])
    gs.add_mentions("f2", [{"name": "Acme", "kind": "org"}])

    nbrs = gs.neighbors("f1")
    matches = [n for n in nbrs if n["file_id"] == "f2"]
    assert len(matches) == 1
    assert matches[0]["via"] == "mentioned-entity"
    assert matches[0]["name"] == "Acme"


def test_references(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    # f2 doesn't exist yet on first call — skip
    gs.add_reference("f1", "f2")

    nbrs = gs.neighbors("f1")
    refs = [n for n in nbrs if n["via"] == "references"]
    assert len(refs) == 1
    assert refs[0]["file_id"] == "f2"


def test_about_shared_topic(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    gs.add_about("f1", ["AI"])
    gs.add_about("f2", ["AI"])

    nbrs = gs.neighbors("f1")
    matches = [n for n in nbrs if n["file_id"] == "f2"]
    assert len(matches) >= 1
    # At least one should be via shared-topic
    topics = [n for n in matches if n["via"] == "shared-topic"]
    assert len(topics) == 1
    assert topics[0]["name"] == "AI"


def test_delete_by_file_removes_node(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.add_mentions("f1", [{"name": "Acme", "kind": "org"}])

    gs.delete_by_file("f1")
    assert gs.get_file("f1") is None


def test_duplicate(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    gs.add_duplicate("f1", "f2", 0.95)

    nbrs = gs.neighbors("f1")
    dups = [n for n in nbrs if n["via"] == "duplicate"]
    assert len(dups) == 1
    assert dups[0]["file_id"] == "f2"
    assert dups[0]["score"] == 0.95


def test_neighbors_hops_other_than_1_raises(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    import pytest

    with pytest.raises(NotImplementedError):
        gs.neighbors("f1", hops=2)


def _pair(gs, w1=1.0, w2=1.0):
    """Two files, both mentioning 'Acme' at the given edge weights."""
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    gs.add_mentions("f1", [{"name": "Acme", "kind": "org"}], weight=w1)
    gs.add_mentions("f2", [{"name": "Acme", "kind": "org"}], weight=w2)


def test_neighbors_rows_carry_weight_and_hops(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    _pair(gs, w1=0.8, w2=0.6)

    row = [n for n in gs.neighbors("f1") if n["file_id"] == "f2"][0]
    assert "weight" in row
    assert "hops" in row
    assert row["hops"] == 1
    # Connector strength is bounded by the weaker of the two edges.
    assert row["weight"] == 0.6


def test_neighbors_min_weight_filters_weak_edges(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    _pair(gs, w1=0.2, w2=0.2)

    assert [n for n in gs.neighbors("f1") if n["file_id"] == "f2"]  # ungated: present
    gated = [n for n in gs.neighbors("f1", min_weight=0.35) if n["file_id"] == "f2"]
    assert gated == []  # below gate: filtered


def test_neighbors_min_weight_keeps_strong_edges(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    _pair(gs, w1=0.9, w2=0.9)

    kept = [n for n in gs.neighbors("f1", min_weight=0.35) if n["file_id"] == "f2"]
    assert len(kept) == 1


def test_neighbors_min_weight_does_not_gate_references(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(
        id="f1",
        path="/a.md",
        name="a.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=1.0,
        content_hash="",
        summary=None,
    )
    f2 = FileRecord(
        id="f2",
        path="/b.md",
        name="b.md",
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=2.0,
        content_hash="",
        summary=None,
    )
    gs.upsert_file(f1)
    gs.upsert_file(f2)
    gs.add_reference("f1", "f2")

    # Structural edges are explicit, not gated by min_weight.
    refs = [n for n in gs.neighbors("f1", min_weight=0.99) if n["via"] == "references"]
    assert len(refs) == 1
