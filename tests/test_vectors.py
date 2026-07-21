"""Tests for VectorStore (LanceDB-backed)."""

from __future__ import annotations

from katsi_core.models import Chunk
from katsi_core.store.vectors import VectorStore


def test_init_creates_table(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=8)
    assert vs.count() == 0
    # Second call is a no-op
    vs.init_table(embed_dim=8)
    assert vs.count() == 0


def test_upsert_and_search(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="hello world", token_count=2),
        Chunk(id="c2", file_id="f1", ordinal=1, text="goodbye world", token_count=2),
    ]
    vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    vs.upsert_chunks(chunks, vectors)

    # Search with the first vector — c1 should be first
    results = vs.search([1.0, 0.0, 0.0, 0.0], k=2)
    assert len(results) == 2
    assert results[0][0] == "c1"  # chunk_id
    assert results[0][1] == "f1"  # file_id


def test_upsert_replaces_by_file_id(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks_a = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1),
        Chunk(id="c2", file_id="f1", ordinal=1, text="b", token_count=1),
    ]
    vs.upsert_chunks(chunks_a, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
    assert vs.count() == 2

    chunks_b = [
        Chunk(id="c3", file_id="f1", ordinal=0, text="c", token_count=1),
    ]
    vs.upsert_chunks(chunks_b, [[0.5, 0.5, 0.0, 0.0]])
    # Old chunks replaced; count should match latest set
    assert vs.count() == 1


def test_delete_by_file(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1),
        Chunk(id="c2", file_id="f1", ordinal=1, text="b", token_count=1),
    ]
    vs.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
    assert vs.count() == 2

    vs.delete_by_file("f1")
    assert vs.count() == 0


def test_search_returns_three_tuple(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="hello", token_count=1),
    ]
    vs.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0]])
    results = vs.search([1.0, 0.0, 0.0, 0.0], k=5)

    assert len(results) == 1
    cid, fid, score = results[0]
    assert isinstance(cid, str)
    assert isinstance(fid, str)
    assert isinstance(score, float)
    assert fid == "f1"


def test_empty_upsert_is_noop(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)
    vs.upsert_chunks([], [])  # should not raise
    assert vs.count() == 0


def test_upsert_mismatched_lengths_raises(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)
    import pytest
    with pytest.raises(ValueError, match="len.*!=.*len"):
        vs.upsert_chunks(
            [Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1)],
            [],
        )
