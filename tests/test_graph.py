"""Tests for GraphStore (Kùzu-backed)."""

from __future__ import annotations

from mnemo_core.models import FileRecord, IndexStatus
from mnemo_core.store.graph import GraphStore


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


def test_mentions_and_peers(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(id="f1", path="/a.md", name="a.md", ext=".md", mime="",
                    size_bytes=0, mtime=1.0, content_hash="", summary=None)
    f2 = FileRecord(id="f2", path="/b.md", name="b.md", ext=".md", mime="",
                    size_bytes=0, mtime=2.0, content_hash="", summary=None)
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
    f1 = FileRecord(id="f1", path="/a.md", name="a.md", ext=".md", mime="",
                    size_bytes=0, mtime=1.0, content_hash="", summary=None)
    f2 = FileRecord(id="f2", path="/b.md", name="b.md", ext=".md", mime="",
                    size_bytes=0, mtime=2.0, content_hash="", summary=None)
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
    f1 = FileRecord(id="f1", path="/a.md", name="a.md", ext=".md", mime="",
                    size_bytes=0, mtime=1.0, content_hash="", summary=None)
    f2 = FileRecord(id="f2", path="/b.md", name="b.md", ext=".md", mime="",
                    size_bytes=0, mtime=2.0, content_hash="", summary=None)
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
    f1 = FileRecord(id="f1", path="/a.md", name="a.md", ext=".md", mime="",
                    size_bytes=0, mtime=1.0, content_hash="", summary=None)
    gs.upsert_file(f1)
    gs.add_mentions("f1", [{"name": "Acme", "kind": "org"}])

    gs.delete_by_file("f1")
    assert gs.get_file("f1") is None


def test_duplicate(tmp_path):
    gs = GraphStore(tmp_path / "graph")
    f1 = FileRecord(id="f1", path="/a.md", name="a.md", ext=".md", mime="",
                    size_bytes=0, mtime=1.0, content_hash="", summary=None)
    f2 = FileRecord(id="f2", path="/b.md", name="b.md", ext=".md", mime="",
                    size_bytes=0, mtime=2.0, content_hash="", summary=None)
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
