from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.clients.llm import LLMClient
from mnemo_core.config import get_settings
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.models import ContextBundle, FileHit, FileRecord
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
def related(file_id: str, kinds: list[str] | None = None) -> list[FileHit]:
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
    return svc["pipeline"].index_file(Path(path))


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
