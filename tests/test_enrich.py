"""Tests for katsi_core.ingest.enrich.

No network calls. Uses real GraphStore with tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from katsi_core.ingest.enrich import apply_extraction, project_chunks
from katsi_core.models import Chunk, Extraction, FileRecord, IndexStatus
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


def _make_record(
    tmp_path: Path,
    file_id: str,
    name: str = "x.md",
    summary: str = "test doc",
) -> FileRecord:
    return FileRecord(
        id=file_id,
        path=str(tmp_path / name),
        name=name,
        ext=".md",
        mime="text/markdown",
        size_bytes=100,
        mtime=1000.0,
        content_hash="abc",
        status=IndexStatus.INDEXED,
        summary=summary,
    )


def test_apply_extraction_creates_entities_and_topics(tmp_path):
    """File F1 with entities and topics -> graph has Entity + Topic nodes."""
    graph = GraphStore(tmp_path / "graph")
    f1_id = "f1"
    record = _make_record(tmp_path, f1_id, "a.md")
    extraction = Extraction(
        summary="doc summary",
        entities=[{"name": "Acme", "kind": "org"}],
        topics=["ai"],
        references=[],
    )

    apply_extraction(record, extraction, graph)

    # Entity node exists
    res = graph._conn.execute("MATCH (e:Entity {name:$name}) RETURN e.kind", {"name": "Acme"})
    assert res.has_next()
    row = res.get_next()
    kind = row[0].value if hasattr(row[0], "value") else row[0]
    assert kind == "org"

    # Topic node exists
    res = graph._conn.execute("MATCH (t:Topic {name:$name}) RETURN t.name", {"name": "ai"})
    assert res.has_next()

    # MENTIONS edge
    res = graph._conn.execute(
        "MATCH (f:File {id:$fid})-[:MENTIONS]->(e:Entity {name:$ename}) RETURN e.name",
        {"fid": f1_id, "ename": "Acme"},
    )
    assert res.has_next()

    # ABOUT edge
    res = graph._conn.execute(
        "MATCH (f:File {id:$fid})-[:ABOUT]->(t:Topic {name:$tname}) RETURN t.name",
        {"fid": f1_id, "tname": "ai"},
    )
    assert res.has_next()


def test_apply_extraction_resolves_reference_by_name(tmp_path):
    """File F1 references 'y.md'; pre-insert F2 with name 'y.md' -> REFERENCES edge."""
    graph = GraphStore(tmp_path / "graph")
    f1_id = "f1"
    f2_id = "f2"

    # Pre-insert the referenced file
    f2 = _make_record(tmp_path, f2_id, "y.md")
    graph.upsert_file(f2)

    record = _make_record(tmp_path, f1_id, "a.md")
    extraction = Extraction(
        summary="doc summary",
        entities=[],
        topics=[],
        references=["y.md"],
    )

    apply_extraction(record, extraction, graph)

    # REFERENCES edge F1 -> F2
    res = graph._conn.execute(
        "MATCH (src:File {id:$src})-[:REFERENCES]->(dst:File {id:$dst}) RETURN dst.id",
        {"src": f1_id, "dst": f2_id},
    )
    assert res.has_next()


def test_apply_extraction_skips_unresolvable_reference(tmp_path):
    """Reference to nonexistent.md -> enriched without crashing, no edge added."""
    graph = GraphStore(tmp_path / "graph")
    f1_id = "f1"
    record = _make_record(tmp_path, f1_id, "a.md")
    extraction = Extraction(
        summary="doc summary",
        entities=[],
        topics=[],
        references=["nonexistent.md"],
    )

    # Must not raise
    apply_extraction(record, extraction, graph)

    # The file node exists
    assert graph.get_file(f1_id) is not None


def test_apply_extraction_backfills_reference_when_target_arrives_later(tmp_path):
    """Reference edges converge regardless of which file is projected first."""
    graph = GraphStore(tmp_path / "graph")
    source = _make_record(tmp_path, "source", "source.md")
    target = _make_record(tmp_path, "target", "target.md")
    apply_extraction(
        source,
        Extraction(summary="source", entities=[], topics=[], references=["target.md"]),
        graph,
    )
    assert graph.neighbors("source") == []

    apply_extraction(
        target,
        Extraction(summary="target", entities=[], topics=[], references=[]),
        graph,
    )
    assert [neighbor["file_id"] for neighbor in graph.neighbors("source")] == ["target"]


def test_reference_backfill_refuses_ambiguous_basename(tmp_path):
    """Duplicate basenames must not make reference resolution order-dependent."""
    graph = GraphStore(tmp_path / "graph")
    source = _make_record(tmp_path, "source", "source.md")
    left = _make_record(tmp_path, "left", "one/target.md")
    right = _make_record(tmp_path, "right", "two/target.md")
    for record, references in ((source, ["target.md"]), (left, []), (right, [])):
        apply_extraction(
            record,
            Extraction(summary=record.id, entities=[], topics=[], references=references),
            graph,
        )

    assert graph.neighbors("source") == []


def test_apply_extraction_idempotent(tmp_path):
    """Call apply_extraction twice -> same nodes (counts unchanged on second call)."""
    graph = GraphStore(tmp_path / "graph")
    f1_id = "f1"
    record = _make_record(tmp_path, f1_id, "a.md")
    extraction = Extraction(
        summary="doc summary",
        entities=[{"name": "Acme", "kind": "org"}],
        topics=["ai"],
        references=[],
    )

    apply_extraction(record, extraction, graph)

    # Count nodes/edges
    r1 = graph._conn.execute("MATCH (e:Entity) RETURN count(*)")
    entity_count_1 = r1.get_next()[0]

    r2 = graph._conn.execute("MATCH ()-[:MENTIONS]->() RETURN count(*)")
    mentions_count_1 = r2.get_next()[0]

    # Apply again
    apply_extraction(record, extraction, graph)

    r1b = graph._conn.execute("MATCH (e:Entity) RETURN count(*)")
    entity_count_2 = r1b.get_next()[0]
    assert entity_count_2 == entity_count_1

    r2b = graph._conn.execute("MATCH ()-[:MENTIONS]->() RETURN count(*)")
    mentions_count_2 = r2b.get_next()[0]
    assert mentions_count_2 == mentions_count_1


def test_reapplying_extraction_replaces_stale_current_relationships(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    record = _make_record(tmp_path, "f1", "current.md")
    apply_extraction(
        record,
        Extraction(
            summary="first",
            entities=[{"name": "Old", "kind": "project"}],
            topics=["old-topic"],
            references=[],
        ),
        graph,
    )
    apply_extraction(
        record,
        Extraction(
            summary="second",
            entities=[{"name": "New", "kind": "project"}],
            topics=["new-topic"],
            references=[],
        ),
        graph,
    )

    old_edges = graph._conn.execute(
        "MATCH (:File {id: $id})-[:MENTIONS]->(:Entity {name: 'Old'}) RETURN 1",
        {"id": record.id},
    )
    old_topics = graph._conn.execute(
        "MATCH (:File {id: $id})-[:ABOUT]->(:Topic {name: 'old-topic'}) RETURN 1",
        {"id": record.id},
    )
    new_edges = graph._conn.execute(
        "MATCH (:File {id: $id})-[:MENTIONS]->(:Entity {name: 'New'}) RETURN 1",
        {"id": record.id},
    )
    assert not old_edges.has_next()
    assert not old_topics.has_next()
    assert new_edges.has_next()


def test_project_chunks_replaces_current_chunks(tmp_path):
    """Re-projecting a resource replaces its previous current chunks."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    record = _make_record(tmp_path, "f1", "a.md")

    first = [
        Chunk(id="f1:0", file_id="f1", ordinal=0, text="a", token_count=1),
        Chunk(id="f1:1", file_id="f1", ordinal=1, text="b", token_count=1),
    ]
    project_chunks(record, first, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], vectors)
    assert vectors.count() == 2

    second = [Chunk(id="f1:0", file_id="f1", ordinal=0, text="c", token_count=1)]
    project_chunks(record, second, [[0.5, 0.5, 0.0, 0.0]], vectors)
    # Old chunks replaced; only the new current set remains.
    assert vectors.count() == 1


def test_project_chunks_excludes_errored_resource(tmp_path):
    """An errored resource's previous chunks are removed and none published."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    indexed = _make_record(tmp_path, "f1", "a.md")

    project_chunks(
        indexed,
        [Chunk(id="f1:0", file_id="f1", ordinal=0, text="a", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
        vectors,
    )
    assert vectors.count() == 1

    errored = indexed.model_copy(update={"status": IndexStatus.ERROR})
    project_chunks(
        errored,
        [Chunk(id="f1:0", file_id="f1", ordinal=0, text="b", token_count=1)],
        [[0.0, 1.0, 0.0, 0.0]],
        vectors,
    )
    # The errored resource is excluded: stale chunks gone, none re-published.
    assert vectors.count() == 0


def test_project_chunks_excludes_non_current_resource(tmp_path):
    """A resource leaving current state (e.g. deleted) drops its chunks."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    indexed = _make_record(tmp_path, "f1", "a.md")

    project_chunks(
        indexed,
        [Chunk(id="f1:0", file_id="f1", ordinal=0, text="a", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
        vectors,
    )
    assert vectors.count() == 1

    # A resource no longer current is represented by a non-publishable status.
    pending = indexed.model_copy(update={"status": IndexStatus.PENDING})
    project_chunks(pending, [], [], vectors)
    assert vectors.count() == 0


def test_project_chunks_excluded_resource_is_not_searchable(tmp_path):
    """Deleted or errored resources cannot remain searchable after re-projection."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    indexed = _make_record(tmp_path, "f1", "a.md")

    project_chunks(
        indexed,
        [Chunk(id="f1:0", file_id="f1", ordinal=0, text="a", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
        vectors,
    )
    assert vectors.search([1.0, 0.0, 0.0, 0.0], k=5) != []

    # A later errored re-projection removes the stale chunks and publishes none,
    # so the resource cannot be found through current retrieval.
    errored = indexed.model_copy(update={"status": IndexStatus.ERROR})
    project_chunks(
        errored,
        [Chunk(id="f1:0", file_id="f1", ordinal=0, text="b", token_count=1)],
        [[0.0, 1.0, 0.0, 0.0]],
        vectors,
    )
    assert vectors.search([1.0, 0.0, 0.0, 0.0], k=5) == []
    assert vectors.search([0.0, 1.0, 0.0, 0.0], k=5) == []
