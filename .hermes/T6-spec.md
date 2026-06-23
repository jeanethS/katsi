# T6 — FastMCP server

Extends the existing mnemo workspace. T0–T5 already done — add only the new files.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).

When done run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail outputs.

## 0. Verified FastMCP API (mcp 1.28.x)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mnemo")

@mcp.tool()
def search(query: str, k: int = 8) -> list[dict]:
    """Tool description becomes the MCP tool description."""
    return [{"q": query}]

if __name__ == "__main__":
    mcp.run()    # runs over stdio by default
```

- Function args become the MCP input schema.
- Pydantic models / dataclasses as return types are auto-serialized.
- Docstring is the tool description.
- `mcp.run()` starts the stdio server (entrypoint).

## 1. What you wire together

From `mnemo_core.models`: `FileHit`, `ContextBundle`, `FileRecord`.
From `mnemo_core.config`: `Settings`, `get_settings`.
From `mnemo_core.ingest.pipeline`: `IngestPipeline`.
From `mnemo_core.ingest.records`: `FileRecordStore`.
From `mnemo_core.store.graph`: `GraphStore`.
From `mnemo_core.store.vectors`: `VectorStore` (used indirectly via pipeline/retrieve).
From `mnemo_core.clients.embed`: `EmbedClient`.
From `mnemo_core.clients.llm`: `LLMClient`.
From `mnemo_core.retrieve.search`: `search`.
From `mnemo_core.retrieve.context`: `build_context`.

## 2. Files to create / update (3 files)

```
packages/mcp_server/mnemo_mcp/server.py         (REWRITE the T0 stub)
packages/mcp_server/mnemo_mcp/__init__.py        (replace NotImplementedError stub)
tests/test_mcp_server.py                          (NEW)
```

Do NOT touch any other files (T0–T5 stay untouched).

## 3. Contract: `packages/mcp_server/mnemo_mcp/server.py`

Implements §6 of the architecture spec. Five tools + one config-gated `answer`. Each
tool delegates to `mnemo_core`. Server keeps ONE set of long-lived clients/graph
constructed lazily on first tool call.

```python
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.clients.llm import LLMClient
from mnemo_core.config import Settings, get_settings
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import ContextBundle, FileHit, FileRecord, IndexStatus
from mnemo_core.retrieve.context import build_context
from mnemo_core.retrieve.search import search
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore

logger = logging.getLogger(__name__)

mcp = FastMCP("mnemo")


# --- shared service singletons (lazy-init on first tool call) ---

_state: dict = {}


def _services():
    """Lazily construct + share pipeline, embed, llm, graph, vectors, records."""
    if _state:
        return _state
    s = get_settings()
    _state["settings"] = s
    _state["embed"] = EmbedClient(s)
    _state["llm"] = LLMClient(s)
    _state["graph"] = GraphStore(s.store.data_dir / s.store.kuzu_db)
    _state["vectors"] = VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table)
    _state["records"] = FileRecordStore(s.store.data_dir / "records")
    _state["pipeline"] = IngestPipeline(s,
                                         graph=_state["graph"],
                                         vectors=_state["vectors"],
                                         embed=_state["embed"],
                                         llm=_state["llm"],
                                         records=_state["records"])
    return _state


# --- MCP tools ---


@mcp.tool()
def index_status() -> dict:
    """Counts by status, last index time, total chunks."""
    s = get_settings()
    svc = _services()
    counts = svc["records"].count_by_status()
    total_files = sum(counts.values())
    last_indexed = None
    for rec in svc["records"].list_all():
        if rec.last_indexed_at is not None and (last_indexed is None
                                                  or rec.last_indexed_at > last_indexed):
            last_indexed = rec.last_indexed_at
    try:
        total_chunks = svc["vectors"].count()
    except Exception as e:
        logger.warning("index_status: vector count failed: %r", e)
        total_chunks = 0
    return {
        "counts_by_status": counts,
        "total_files": total_files,
        "total_chunks": total_chunks,
        "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
    }


@mcp.tool()
def search_files(query: str, k: int = 8) -> list[FileHit]:
    """Ranked files for a query, each with a one-line 'why relevant'."""
    # NOTE: tool name is search_files to avoid shadowing the imported search().
    svc = _services()
    return search(query, k=k,
                  settings=svc["settings"], vectors=svc["vectors"],
                  graph=svc["graph"], embed=svc["embed"], records=svc["records"])


@mcp.tool()
def get_context(query: str, max_tokens: int = 3000) -> ContextBundle:
    """PRIMARY TOOL. Curated, budget-capped context for the client to answer over:
    file summaries + the few most relevant raw chunks + a graph relationship sketch."""
    svc = _services()
    return build_context(query, max_tokens=max_tokens,
                          settings=svc["settings"], vectors=svc["vectors"],
                          graph=svc["graph"], embed=svc["embed"],
                          records=svc["records"])


@mcp.tool()
def get_file_summary(file_id: str) -> FileRecord:
    """Cached summary + metadata for one file (no re-read of the file)."""
    svc = _services()
    rec = svc["records"].get(file_id)
    if rec is None:
        # fallback: try graph
        node = svc["graph"].get_file(file_id)
        if node is None:
            raise ValueError(f"unknown file_id: {file_id}")
        return node
    return rec


@mcp.tool()
def related(file_id: str, kinds: Optional[list[str]] = None) -> list[FileHit]:
    """Graph neighbors: shared entities/topics, references, duplicates."""
    svc = _services()
    nbs = svc["graph"].neighbors(file_id, hops=1)
    if kinds:
        nbs = [n for n in nbs if n.get("via") in kinds]
    hits: list[FileHit] = []
    seen: set[str] = set()
    for nb in nbs:
        peer = nb.get("file_id")
        if peer is None or peer in seen or peer == file_id:
            continue
        seen.add(peer)
        node = svc["graph"].get_file(peer)
        if node is None:
            rec = svc["records"].get(peer)
            if rec is None:
                continue
            path = rec.path
            summary = rec.summary or ""
        else:
            path = node.path
            summary = node.summary or ""
        hits.append(FileHit(
            file_id=peer, path=path, summary=summary,
            score=nb.get("score", 0.0), why=nb.get("via", "neighbor"),
        ))
    return hits


@mcp.tool()
def index_file_tool(path: str) -> FileRecord:
    """Index a single file via the ingest pipeline. Useful for ad-hoc ingestion
    from the client without running the CLI."""
    svc = _services()
    return svc["pipeline"].index_file(path)


@mcp.tool()
def answer(query: str) -> str:
    """Local-model synthesis over get_context output. For sensitive trees
    where nothing should leave the machine. OFF BY DEFAULT."""
    svc = _services()
    s = svc["settings"]
    if not s.mcp.enable_answer_tool:
        raise PermissionError(
            "answer tool disabled; set mnemo.mcp.enable_answer_tool=true to enable"
        )
    bundle = build_context(query, max_tokens=s.retrieve.default_context_max_tokens,
                            settings=s, vectors=svc["vectors"], graph=svc["graph"],
                            embed=svc["embed"], records=svc["records"])
    # Render a prompt and have the local LLM synthesize over the bundle.
    prompt_parts = [f"# Context bundle for query: {bundle.query}"]
    prompt_parts.append("\n## Files:")
    for h in bundle.files:
        prompt_parts.append(f"- {h.path} (score={h.score:.3f}; {h.why})")
        if h.summary:
            prompt_parts.append(f"  SUMMARY: {h.summary}")
    prompt_parts.append("\n## Top chunks:")
    for c in bundle.chunks:
        prompt_parts.append(f"--- chunk {c.id} ({c.token_count} tokens) ---")
        prompt_parts.append(c.text)
    if bundle.relationships:
        prompt_parts.append("\n## Relationships:")
        prompt_parts.extend(bundle.relationships)
    prompt_parts.append("\nAnswer the query using ONLY the context above.")
    prompt = "\n".join(prompt_parts)
    return svc["llm"].chat(prompt, temperature=0.2)


def main() -> None:
    """Entry point: `mnemo-mcp` console script."""
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
```

Use this implementation reference VERBATIM. The `index_file_tool` is an addition
beyond §6 — it lets the MCP client trigger ingestion. Keep it.

Note the tool `search_files` shadows nothing because we use a function name distinct
from the `search` import. The MCP tool name (as exposed to clients) is `search_files`.

## 4. Contract: `packages/mcp_server/mnemo_mcp/__init__.py`

```python
"""mnemo MCP server package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `mnemo-mcp` script."""
    from mnemo_mcp.server import main as _real
    _real()
```

The `from mnemo_mcp.server import main as _real` is deferred so that printing
`--help` from a CLI that imports mnemo_mcp does not eagerly start the MCP server.

## 5. Contract: `tests/test_mcp_server.py`

A SMOKE test that initializes against a fixtured store and calls `get_context`
(and a couple other tools). The MCP server is NOT started over stdio — we test
the tool functions directly.

```python
"""Smoke tests for the mnemo FastMCP server tools."""
from __future__ import annotations

from pathlib import Path

import pytest

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.config import Settings, reset_settings
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore


class _FakeEmbed:
    def __init__(self, dim: int = 8):
        self.dim = dim
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[0.5] * self.dim for _ in texts]


class _FakeLLM:
    def __init__(self, json_str: str):
        self.json_str = json_str
        self.calls = 0

    def extract(self, text, *, attempts: int = 2):
        self.calls += 1
        import json as _json
        from mnemo_core.models import Extraction
        return Extraction(**_json.loads(self.json_str))

    def chat(self, prompt, *, temperature: float = 0.2):
        return f"answer for prompt len={len(prompt)}"

    def _chat(self, system_prompt, user_text):
        return ""

    def chat_with_settings(self, *args, **kwargs):
        return ""


EXTRACTION_JSON = '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],"topics":["ai"],"references":[]}'


@pytest.fixture
def server_state(tmp_path):
    """Replace the mcp server `_state` with a fresh fixture-backed pipeline."""
    # Build local stores pointing at tmp_path
    s = Settings()
    # Override data_dir so writes go to tmp, not ~/.mnemo.
    s.store.data_dir = tmp_path / "mnemo_data"
    vectors = VectorStore(tmp_path / "mnemo_data" / "vectors")
    vectors.init_table(8)
    graph = GraphStore(tmp_path / "mnemo_data" / "graph")
    records = FileRecordStore(tmp_path / "mnemo_data" / "records")
    embed = _FakeEmbed(dim=8)
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline = IngestPipeline(s, graph=graph, vectors=vectors, embed=embed,
                              llm=llm, records=records)

    # Import the mcp server module
    from mnemo_mcp import server as srv
    srv._state.clear()
    srv._state.update({
        "settings": s,
        "embed": embed,
        "llm": llm,
        "graph": graph,
        "vectors": vectors,
        "records": records,
        "pipeline": pipeline,
    })
    return srv, embed, llm, records


def test_index_status_counts_zero_when_empty(server_state):
    srv, embed, llm, records = server_state
    res = srv.index_status()
    assert isinstance(res, dict)
    assert res["total_files"] == 0
    assert res["total_chunks"] == 0
    assert res["last_indexed_at"] is None


def test_get_context_returns_empty_bundle_when_unindexed(server_state):
    srv, embed, llm, records = server_state
    b = srv.get_context("anything", max_tokens=500)
    assert b.query == "anything"
    assert b.files == []
    assert b.chunks == []
    assert b.token_estimate == 0


def test_get_file_summary_unknown_raises(server_state):
    srv, embed, llm, records = server_state
    with pytest.raises(ValueError):
        srv.get_file_summary("nonexistent")


def test_related_returns_empty_for_unknown_file(server_state):
    srv, embed, llm, records = server_state
    out = srv.related("nonexistent")
    assert out == []


def test_answer_tool_disabled_by_default_raises(server_state):
    srv, embed, llm, records = server_state
    # Settings.mcp.enable_answer_tool defaults to False
    with pytest.raises(PermissionError):
        srv.answer("any query")


def test_answer_tool_works_when_enabled(server_state):
    srv, embed, llm, records = server_state
    srv._state["settings"].mcp.enable_answer_tool = True
    out = srv.answer("any query")
    assert isinstance(out, str)
    assert out.startswith("answer")


def test_smoke_index_then_get_context(server_state, tmp_path):
    """End-to-end smoke: index a small markdown file via the pipeline, then
    call get_context via the MCP server tool and assert the bundle is non-empty."""
    srv, embed, llm, records = server_state
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Title\n\nThis document mentions Acme and AI.")
    # Index via the pipeline exposed through the server
    rec = srv.index_file_tool(str(md_path))
    assert rec.status.value == "indexed"
    assert embed.calls == 1   # one embed call for the chunks
    assert llm.calls == 1     # one extract call

    # Now get_context should find it
    bundle = srv.get_context("Acme AI", max_tokens=2000)
    assert bundle.query == "Acme AI"
    # at least one file should be in the bundle
    assert len(bundle.files) >= 1
    # second call with same query shouldn't need a new extract (only 1 was made),
    # but embed.embed will be called again for the query vector. Allow that.
```

## 6. Constraints

- Do NOT add new dependencies. mcp is already in mnemo-mcp deps.
- Do NOT modify any T0–T5 files.
- Do NOT leave TODO comments.
- Do NOT start the MCP stdio server in tests (we test the tool functions directly).

## 7. Done when

- All 3 files exist with the contracts above.
- `uv run pytest` passes (existing 64 + ~7 mcp server = ~71+).
- `uv run ruff check .` is clean.
- `uv run python -c "from mnemo_mcp.server import main"` works without raising.
- Hand back a short report.
