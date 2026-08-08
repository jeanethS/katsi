from __future__ import annotations

import pytest

from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import Chunk, FileRecord, IndexStatus
from katsi_core.retrieve.context import build_context
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


class _FakeEmbed:
    def __init__(self, dim=8):
        self.dim = dim
        self.embeds: list[list[str]] = []

    def embed(self, texts):
        self.embeds.append(list(texts))
        return [[0.5] * self.dim for _ in texts]


@pytest.fixture
def setup_stores(tmp_path):
    s = Settings()
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(8)
    graph = GraphStore(tmp_path / "graph")
    records = FileRecordStore(tmp_path / "records")
    return s, vectors, graph, records, _FakeEmbed()


def _index_file_summary(records, graph, file_id, path, summary, content_hash="h"):
    rec = FileRecord(
        id=file_id,
        path=path,
        name=path.split("/")[-1],
        ext=".md",
        mime="",
        size_bytes=0,
        mtime=0.0,
        content_hash=content_hash,
        status=IndexStatus.INDEXED,
        summary=summary,
    )
    records.put(rec)
    graph.upsert_file(rec)
    return rec


def test_build_context_empty_query_returns_empty_bundle(setup_stores):
    s, vectors, graph, records, embed = setup_stores
    bundle = build_context(
        "", max_tokens=3000, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )
    assert bundle.token_estimate == 0
    assert bundle.files == []
    assert bundle.chunks == []
    assert bundle.relationships == []


def test_build_context_never_exceeds_max_tokens(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    small_text = "x" * 300
    chunks = [Chunk(id="f1:0", file_id="f1", ordinal=0, text=small_text, token_count=75)]
    vecs = [[0.5] * 8]
    vectors.upsert_chunks(chunks, vecs)

    bundle = build_context(
        "q", max_tokens=200, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )

    assert bundle.token_estimate <= 200
    assert bundle.chunks == []


def test_build_context_includes_relationships_for_in_bundle_files(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    _index_file_summary(records, graph, "f2", "/docs/b.md", "beta")

    chunks = [
        Chunk(id="f1:0", file_id="f1", ordinal=0, text="text one", token_count=3),
        Chunk(id="f2:0", file_id="f2", ordinal=0, text="text two", token_count=3),
    ]
    vecs = [[0.5] * 8, [0.5] * 8]
    vectors.upsert_chunks(chunks, vecs)

    graph.add_mentions("f1", [{"name": "Acme", "kind": "org"}])
    graph.add_mentions("f2", [{"name": "Acme", "kind": "org"}])

    bundle = build_context(
        "q", max_tokens=3000, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )

    assert len(bundle.relationships) > 0


def test_build_context_dedups_files(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    chunks = [Chunk(id="f1:0", file_id="f1", ordinal=0, text="text", token_count=3)]
    vecs = [[0.5] * 8]
    vectors.upsert_chunks(chunks, vecs)

    bundle = build_context(
        "q", max_tokens=3000, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )

    fids = [h.file_id for h in bundle.files]
    assert len(fids) == len(set(fids))


@pytest.mark.parametrize("max_k", [1, 2])
def test_build_context_returns_at_most_k_files(setup_stores, max_k):
    s, vectors, graph, records, embed = setup_stores

    for i in range(3):
        fid = f"f{i}"
        _index_file_summary(records, graph, fid, f"/docs/{fid}.md", fid)
        chunks = [Chunk(id=f"{fid}:0", file_id=fid, ordinal=0, text=f"text {i}", token_count=3)]
        vecs = [[0.5] * 8]
        vectors.upsert_chunks(chunks, vecs)

    s.retrieve.top_k_files = max_k
    bundle = build_context(
        "q", max_tokens=5000, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )

    assert len(bundle.files) <= max_k


def test_build_context_includes_top_chunk_when_budget_allows(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    small_text = "hello world " * 4
    chunks = [Chunk(id="f1:0", file_id="f1", ordinal=0, text=small_text, token_count=13)]
    vecs = [[0.5] * 8]
    vectors.upsert_chunks(chunks, vecs)

    bundle = build_context(
        "q", max_tokens=500, settings=s, vectors=vectors, graph=graph, embed=embed, records=records
    )

    assert len(bundle.chunks) == 1
    assert bundle.token_estimate <= 500
