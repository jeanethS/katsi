"""Tests for mnemo_core.ingest.enrich.

No network calls. Uses real GraphStore with tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from mnemo_core.ingest.enrich import apply_extraction
from mnemo_core.models import Extraction, FileRecord, IndexStatus
from mnemo_core.store.graph import GraphStore


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
    res = graph._conn.execute(
        "MATCH (e:Entity {name:$name}) RETURN e.kind", {"name": "Acme"}
    )
    assert res.has_next()
    row = res.get_next()
    kind = row[0].value if hasattr(row[0], "value") else row[0]
    assert kind == "org"

    # Topic node exists
    res = graph._conn.execute(
        "MATCH (t:Topic {name:$name}) RETURN t.name", {"name": "ai"}
    )
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
        "MATCH (src:File {id:$src})-[:REFERENCES]->(dst:File {id:$dst}) "
        "RETURN dst.id",
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
