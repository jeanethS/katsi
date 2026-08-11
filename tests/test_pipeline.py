"""Tests for katsi_core.ingest.pipeline.

Critical test: test_second_call_skips_when_unchanged (the saver).
No network calls. Uses fake embed/llm clients that count calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import blake3

from katsi_core.clients.llm import ExtractionError
from katsi_core.config import Settings, SQLiteSettings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import Extraction, IndexStatus
from katsi_core.store.enrichment_cache import EnrichmentCache
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


class _FakeEmbed:
    """Counts every call to embed()."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.embed_call_count = 0

    def embed(self, texts):
        self.embed_call_count += 1
        return [[0.5] * self.dim for _ in texts]


class _FakeEmbedError:
    """Always fails embedding, simulating a vector-projection failure."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.embed_call_count = 0

    def embed(self, texts):
        self.embed_call_count += 1
        raise RuntimeError("embed failure")


class _FakeOllama:
    def __init__(self, content: str):
        self.message = SimpleNamespace(content=content)


class _FakeLLM:
    def __init__(self, json_str: str):
        self.json_str = json_str
        self.extract_call_count = 0

    def extract(self, text, *, attempts=2):
        self.extract_call_count += 1
        d = json.loads(self.json_str)
        return Extraction(**d)


class _FakeLLMError:
    def __init__(self):
        self.extract_call_count = 0

    def extract(self, text, *, attempts=2):
        self.extract_call_count += 1
        raise ExtractionError("fake failure")


EXTRACTION_JSON = (
    '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],'
    '"topics":["ai"],"references":[]}'
)


def make_pipeline(tmp_path, embed, llm, enrichment_cache=None):
    s = GraphStore(tmp_path / "graph")
    v = VectorStore(tmp_path / "vectors")
    r = FileRecordStore(tmp_path / "records")
    p = IngestPipeline(
        settings=None,
        graph=s,
        vectors=v,
        embed=embed,
        llm=llm,
        records=r,
        enrichment_cache=enrichment_cache,
    )
    return p, s, v, r


def _write_file(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_index_file_processes_a_markdown_file(tmp_path):
    """index_file(x) returns FileRecord with status=INDEXED, summary, last_indexed_at."""
    p = _write_file(tmp_path / "x.md", "# Hello\n\nThis is a test document.\n")
    embed = _FakeEmbed()
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline, graph, _, _ = make_pipeline(tmp_path, embed, llm)

    result = pipeline.index_file(p)

    assert result.status == IndexStatus.INDEXED
    assert result.summary == "doc summary"
    assert result.last_indexed_at is not None
    assert embed.embed_call_count == 1
    assert llm.extract_call_count == 1

    # Graph has the file via upsert_file
    graph_file = graph.get_file(result.id)
    assert graph_file is not None
    assert graph_file.summary == "doc summary"


def test_second_call_skips_when_unchanged(tmp_path):
    """Same file, same content -> saver: no additional embed or LLM calls.

    This is THE saver test.
    """
    p = _write_file(tmp_path / "x.md", "# Hello\n\nTest content.\n")
    embed = _FakeEmbed()
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm)

    # First call: process
    first_result = pipeline.index_file(p)
    assert first_result.status == IndexStatus.INDEXED
    assert embed.embed_call_count == 1
    assert llm.extract_call_count == 1

    # Second call: should skip (unchanged)
    second_result = pipeline.index_file(p)
    assert second_result.status == IndexStatus.INDEXED
    assert embed.embed_call_count == 1, f"Expected embed_call_count=1, got {embed.embed_call_count}"
    assert llm.extract_call_count == 1, (
        f"Expected extract_call_count=1, got {llm.extract_call_count}"
    )


def test_index_file_marks_error_on_empty_text(tmp_path):
    """Empty file -> status=ERROR, no embed/LLM calls."""
    p = _write_file(tmp_path / "empty.md", "")
    embed = _FakeEmbed()
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm)

    result = pipeline.index_file(p)

    assert result.status == IndexStatus.ERROR
    assert embed.embed_call_count == 0
    assert llm.extract_call_count == 0


def test_index_file_marks_error_before_semantic_projection_on_extraction_failure(tmp_path):
    """An invalid extraction persists an error without publishing vector data."""
    p = _write_file(tmp_path / "x.md", "# Hello\n\nSome content.\n")
    embed = _FakeEmbed()
    llm = _FakeLLMError()
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=1)
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm, EnrichmentCache(database))

    result = pipeline.index_file(p)

    assert result.status == IndexStatus.ERROR
    assert result.error is not None
    assert "extraction error" in result.error
    assert embed.embed_call_count == 0
    assert llm.extract_call_count == 1
    with database.connection() as connection:
        assert (
            connection.execute("SELECT status FROM content_enrichments").fetchone()["status"]
            == "error"
        )


def test_compatible_content_reuses_cached_extraction_across_paths_and_histories(tmp_path):
    """Copied content and A→B→A do not re-run local extraction."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=1)
    cache = EnrichmentCache(database)
    first_path = _write_file(tmp_path / "first.md", "# Same\n\nshared content\n")
    second_path = _write_file(tmp_path / "second.md", "# Same\n\nshared content\n")
    embed = _FakeEmbed()
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm, cache)

    first = pipeline.index_file(first_path)
    copied = pipeline.index_file(second_path)
    _write_file(first_path, "# Different\n\ncontent\n")
    pipeline.index_file(first_path)
    _write_file(first_path, "# Same\n\nshared content\n")
    returned = pipeline.index_file(first_path)

    assert first.status == copied.status == returned.status == IndexStatus.INDEXED
    assert llm.extract_call_count == 2


def test_changed_enrichment_fingerprint_intentionally_reenriches_content(tmp_path):
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=1)
    cache = EnrichmentCache(database)
    first_path = _write_file(tmp_path / "first.md", "# Same\n\nshared content\n")
    second_path = _write_file(tmp_path / "second.md", "# Same\n\nshared content\n")
    graph = GraphStore(tmp_path / "graph")
    vectors = VectorStore(tmp_path / "vectors")
    records = FileRecordStore(tmp_path / "records")
    first_llm = _FakeLLM(EXTRACTION_JSON)
    IngestPipeline(
        graph=graph,
        vectors=vectors,
        embed=_FakeEmbed(),
        llm=first_llm,
        records=records,
        enrichment_cache=cache,
    ).index_file(first_path)

    changed_llm = _FakeLLM(EXTRACTION_JSON)
    changed_settings = Settings(ollama={"llm_model": "different-local-model"})
    second = IngestPipeline(
        settings=changed_settings,
        graph=graph,
        vectors=vectors,
        embed=_FakeEmbed(),
        llm=changed_llm,
        records=records,
        enrichment_cache=cache,
    ).index_file(second_path)

    assert second.status == IndexStatus.INDEXED
    assert first_llm.extract_call_count == 1
    assert changed_llm.extract_call_count == 1


def test_terminal_extraction_error_removes_previous_current_projections(tmp_path):
    path = _write_file(tmp_path / "x.md", "# First\n\ncontent\n")
    embed = _FakeEmbed()
    graph = GraphStore(tmp_path / "graph")
    vectors = VectorStore(tmp_path / "vectors")
    records = FileRecordStore(tmp_path / "records")
    successful = IngestPipeline(
        graph=graph,
        vectors=vectors,
        embed=embed,
        llm=_FakeLLM(EXTRACTION_JSON),
        records=records,
    )
    first = successful.index_file(path)
    assert graph.get_file(first.id) is not None
    assert vectors.count() > 0

    _write_file(path, "# Changed\n\ncontent\n")
    failed = IngestPipeline(
        graph=graph,
        vectors=vectors,
        embed=_FakeEmbed(),
        llm=_FakeLLMError(),
        records=records,
    ).index_file(path)

    assert failed.status == IndexStatus.ERROR
    assert graph.get_file(first.id) is None
    assert vectors.count() == 0


def test_vector_projection_failure_excludes_resource_from_current_projections(tmp_path):
    """A resource whose vector projection fails is excluded from current projections."""
    path = _write_file(tmp_path / "x.md", "# First\n\ncontent\n")
    graph = GraphStore(tmp_path / "graph")
    vectors = VectorStore(tmp_path / "vectors")
    records = FileRecordStore(tmp_path / "records")
    successful = IngestPipeline(
        graph=graph,
        vectors=vectors,
        embed=_FakeEmbed(),
        llm=_FakeLLM(EXTRACTION_JSON),
        records=records,
    )
    first = successful.index_file(path)
    assert first.status == IndexStatus.INDEXED
    assert vectors.count() > 0
    assert graph.get_file(first.id) is not None

    _write_file(path, "# Changed\n\ncontent\n")
    failed = IngestPipeline(
        graph=graph,
        vectors=vectors,
        embed=_FakeEmbedError(),
        llm=_FakeLLM(EXTRACTION_JSON),
        records=records,
    ).index_file(path)

    assert failed.status == IndexStatus.ERROR
    assert failed.error is not None
    assert "embed/vector failure" in failed.error
    # The errored resource is excluded from current search and relationships.
    assert vectors.count() == 0
    assert graph.get_file(first.id) is None


def test_index_file_reindexes_when_content_changes(tmp_path):
    """File modified -> re-indexed with new content_hash, additional embed/extract."""
    p = _write_file(tmp_path / "x.md", "# Version A\n\nOriginal content.\n")
    embed = _FakeEmbed()
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm)

    first = pipeline.index_file(p)
    first_hash = first.content_hash

    # Modify file
    _write_file(tmp_path / "x.md", "# Version B\n\nModified content for reindex.\n")
    second = pipeline.index_file(p)

    assert second.content_hash != first_hash
    assert embed.embed_call_count == 2
    assert llm.extract_call_count == 2


def test_record_store_persists_across_pipeline_instances(tmp_path):
    """Pipeline1 indexes, pipeline2 with same records dir sees it -> saver on second."""
    p = _write_file(tmp_path / "x.md", "# Hello\n\nTest persist.\n")
    records_dir = tmp_path / "records"
    embed1 = _FakeEmbed()
    llm1 = _FakeLLM(EXTRACTION_JSON)
    graph1 = GraphStore(tmp_path / "graph")
    vectors1 = VectorStore(tmp_path / "vectors")
    records1 = FileRecordStore(records_dir)

    pipe1 = IngestPipeline(
        settings=None,
        graph=graph1,
        vectors=vectors1,
        embed=embed1,
        llm=llm1,
        records=records1,
    )
    result1 = pipe1.index_file(p)
    assert result1.status == IndexStatus.INDEXED

    # Destroy the first pipeline to release Kuza file lock
    del graph1
    del vectors1
    del pipe1

    # New pipeline, same records_dir
    embed2 = _FakeEmbed()
    llm2 = _FakeLLM(EXTRACTION_JSON)
    graph2 = GraphStore(tmp_path / "graph")
    vectors2 = VectorStore(tmp_path / "vectors")
    records2 = FileRecordStore(records_dir)

    pipe2 = IngestPipeline(
        settings=None,
        graph=graph2,
        vectors=vectors2,
        embed=embed2,
        llm=llm2,
        records=records2,
    )
    result2 = pipe2.index_file(p)

    assert result2.status == IndexStatus.INDEXED
    # Pipeline2 hits the saver — no embed/LLM calls
    assert embed2.embed_call_count == 0, (
        f"Expected embed_call_count=0 (saver), got {embed2.embed_call_count}"
    )
    assert llm2.extract_call_count == 0, (
        f"Expected extract_call_count=0 (saver), got {llm2.extract_call_count}"
    )

    # Verify via records store directly
    file_id = blake3.blake3(str(p.resolve()).encode("utf-8")).hexdigest()
    stored = records2.get(file_id)
    assert stored is not None
    assert stored.status == IndexStatus.INDEXED
