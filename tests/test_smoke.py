"""Smoke tests for T0 scaffold: imports + model construction."""

from katsi_core.config import Settings
from katsi_core.models import (
    Chunk,
    ContextBundle,
    Extraction,
    FileHit,
    FileRecord,
    IndexStatus,
)


def test_imports_core():
    """All katsi_core public symbols can be imported."""
    assert FileRecord is not None
    assert Chunk is not None
    assert Extraction is not None
    assert FileHit is not None
    assert ContextBundle is not None
    assert IndexStatus.INDEXED == "indexed"


def test_filerecord_construction():
    rec = FileRecord(
        id="abc123",
        path="/tmp/x.md",
        name="x.md",
        ext=".md",
        mime="text/markdown",
        size_bytes=10,
        mtime=1700000000.0,
        content_hash="hash",
    )
    assert rec.status == IndexStatus.PENDING
    assert rec.summary is None
    assert rec.last_indexed_at is None
    assert rec.error is None


def test_chunk_construction():
    c = Chunk(id="abc:0", file_id="abc", ordinal=0, text="hello", token_count=1)
    assert c.id == "abc:0"


def test_extraction_construction():
    e = Extraction(summary="s", entities=[], topics=[], references=[])
    assert e.summary == "s"
    assert e.entities == []


def test_settings_defaults():
    s = Settings()
    assert s.ollama.embed_model == "bge-m3"
    assert s.ollama.llm_model == "qwen2.5:7b"
    assert s.ingest.chunk_token_target == 512
    assert s.ingest.chunk_token_overlap == 64
    assert s.retrieve.default_context_max_tokens == 3000
    assert s.mcp.enable_answer_tool is False


def test_filehit_and_bundle_construction():
    h = FileHit(file_id="f", path="/p", summary="s", score=0.5, why="because")
    assert h.why == "because"
    b = ContextBundle(query="q", files=[h], chunks=[], relationships=[], token_estimate=10)
    assert b.files == [h]
