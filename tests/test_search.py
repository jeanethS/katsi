from __future__ import annotations

import pytest

from mnemo_core.config import Settings
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import Chunk, FileRecord, IndexStatus
from mnemo_core.retrieve.search import WHY_ENTITY, WHY_VECTOR, search
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore


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
        id=file_id, path=path, name=path.split("/")[-1],
        ext=".md", mime="", size_bytes=0, mtime=0.0,
        content_hash=content_hash, status=IndexStatus.INDEXED,
        summary=summary,
    )
    records.put(rec)
    graph.upsert_file(rec)
    return rec


def test_search_empty_query_returns_empty(setup_stores):
    s, vectors, graph, records, embed = setup_stores
    result = search("", k=8, settings=s, vectors=vectors,
                     graph=graph, embed=embed, records=records)
    assert result == []


def test_search_returns_vector_hits_in_order(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    _index_file_summary(records, graph, "f2", "/docs/b.md", "beta")

    chunks = [
        Chunk(id="f1:0", file_id="f1", ordinal=0, text="close match", token_count=3),
        Chunk(id="f2:0", file_id="f2", ordinal=0, text="distant match", token_count=3),
    ]
    vecs = [[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
    vectors.upsert_chunks(chunks, vecs)

    result = search("test", k=8, settings=s, vectors=vectors,
                     graph=graph, embed=embed, records=records)

    assert len(result) == 2
    assert result[0].file_id == "f1"
    assert result[1].file_id == "f2"
    assert result[0].score >= result[1].score
    assert result[0].why == WHY_VECTOR
    assert result[1].why == WHY_VECTOR


def test_search_surfaces_graph_neighbors(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    _index_file_summary(records, graph, "f2", "/docs/b.md", "beta")

    chunks = [
        Chunk(id="f1:0", file_id="f1", ordinal=0, text="close match", token_count=3),
    ]
    vecs = [[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]]
    vectors.upsert_chunks(chunks, vecs)

    graph.add_mentions("f1", [{"name": "Acme", "kind": "org"}])
    graph.add_mentions("f2", [{"name": "Acme", "kind": "org"}])

    result = search("test", k=8, settings=s, vectors=vectors,
                     graph=graph, embed=embed, records=records)

    fids = {h.file_id for h in result}
    assert "f1" in fids
    assert "f2" in fids

    f2_hit = [h for h in result if h.file_id == "f2"][0]
    assert "graph-extended" in f2_hit.why or WHY_ENTITY in f2_hit.why


def test_search_fused_score_better_than_pure_vector(setup_stores):
    s, vectors, graph, records, embed = setup_stores

    s.retrieve.vector_weight = 0.6
    s.retrieve.graph_weight = 0.4

    _index_file_summary(records, graph, "f1", "/docs/a.md", "alpha")
    _index_file_summary(records, graph, "f2", "/docs/b.md", "beta")

    chunks = [
        Chunk(id="f1:0", file_id="f1", ordinal=0, text="close match", token_count=3),
    ]
    vecs = [[0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]]
    vectors.upsert_chunks(chunks, vecs)

    graph.add_mentions("f1", [{"name": "Acme", "kind": "org"}])
    graph.add_mentions("f2", [{"name": "Acme", "kind": "org"}])

    result = search("test", k=8, settings=s, vectors=vectors,
                     graph=graph, embed=embed, records=records)

    assert len(result) == 2
    # f1: 0.6 * 1.0 + 0.4 * 0.0 = 0.6
    # f2: 0.6 * 0.0 + 0.4 * 1.0 = 0.4
    assert result[0].file_id == "f1"
    assert result[1].file_id == "f2"
    assert result[0].score > result[1].score
