"""Tests for mnemo_core.ingest.pipeline.

Critical test: test_second_call_skips_when_unchanged (the saver).
No network calls. Uses fake embed/llm clients that count calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import blake3

from mnemo_core.clients.llm import ExtractionError
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import Extraction, IndexStatus
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore


class _FakeEmbed:
    """Counts every call to embed()."""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.embed_call_count = 0

    def embed(self, texts):
        self.embed_call_count += 1
        return [[0.5] * self.dim for _ in texts]


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


def make_pipeline(tmp_path, embed, llm):
    s = GraphStore(tmp_path / "graph")
    v = VectorStore(tmp_path / "vectors")
    r = FileRecordStore(tmp_path / "records")
    p = IngestPipeline(
        settings=None,
        graph=s, vectors=v, embed=embed, llm=llm, records=r,
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
    assert embed.embed_call_count == 1, (
        f"Expected embed_call_count=1, got {embed.embed_call_count}"
    )
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


def test_index_file_marks_error_on_extraction_failure(tmp_path):
    """LLM raises ExtractionError -> status=ERROR, embed happened, LLM called."""
    p = _write_file(tmp_path / "x.md", "# Hello\n\nSome content.\n")
    embed = _FakeEmbed()
    llm = _FakeLLMError()
    pipeline, _, _, _ = make_pipeline(tmp_path, embed, llm)

    result = pipeline.index_file(p)

    assert result.status == IndexStatus.ERROR
    assert result.error is not None
    assert "extraction error" in result.error
    assert embed.embed_call_count == 1
    assert llm.extract_call_count == 1


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
        graph=graph1, vectors=vectors1,
        embed=embed1, llm=llm1, records=records1,
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
        graph=graph2, vectors=vectors2,
        embed=embed2, llm=llm2, records=records2,
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
