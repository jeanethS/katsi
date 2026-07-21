# katsi — Architecture & Build Spec

> Working name: **MyFoldersote** (placeholder — rename freely). A local-first, privacy-first MCP server that gives any MCP client (Claude Desktop, Code, Cursor, etc.) cheap, relational context about a user's files. The engine does the expensive "understanding" once per file with local models and banks it in a knowledge graph + vector store; at query time it returns a small curated context bundle so the *client's* model synthesizes over a tiny window instead of exploring the filesystem.

This document is written for handoff to an agentic coder (opencode). It contains the architecture, schemas, the MCP interface, the pipelines, an `AGENTS.md` stub, and a phased, dependency-ordered task list where each task is self-contained with acceptance criteria and a paste-ready prompt.

---

## 1. Core value proposition (the thing every decision serves)

Naive filesystem MCP servers dump files into the client's context (the Cowork token problem). Naive RAG servers only do vector similarity. **katsi gives context that is both cheap *and* relational** — summarize-once + a local knowledge graph. The graph is the differentiator; lead with it.

The token math: exploration cost moves from query-time (expensive, cloud, repeated) to ingest-time (cheap, local, once per file change). The only cloud cost is the client synthesizing over a curated bundle.

---

## 2. v0.1 scope

**In:**
- Index a configured set of directories (text, markdown, code, PDF, docx).
- Summarize-once per file with a local model; cache, invalidate on content-hash change.
- Local embeddings + local entity/relation extraction into a knowledge graph.
- MCP server exposing `search`, `get_context`, `get_file_summary`, `related`, `index_status`.
- A CLI (`index`, `status`, `search`, `ask`) as the dogfooding + demo + test surface.
- One-command run via `uvx`.

**Deferred (roadmap, do not build yet):**
- File watcher / live re-indexing (v0.1 indexes on demand; watcher is Phase 4 but optional for first ship).
- Folder/project hierarchical rollups.
- Agentic multi-hop retrieval via LangGraph (v0.1 retrieval is deterministic).
- Local reranker model (v0.1 uses score fusion).
- Standalone GUI.
- Image captioning / OCR.

Ship the wedge: *"ask questions about your folder, locally, for pennies."* Everything else is the roadmap.

---

## 3. Tech stack (locked)

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Smoothest path for every dependency below + future LangGraph. |
| Tooling / dist | `uv` + `uvx` | Fast installs, one-command run, registry-friendly entrypoint. |
| MCP framework | FastMCP (official `mcp` SDK) | Decorator-based tools, registry-publishable metadata. |
| Embeddings | Ollama `bge-m3` | Local, free, multilingual (ES/EN/ZH). |
| Vector store | LanceDB | Embedded, no server, fast, on-disk. |
| Graph store | Kùzu | Embedded, Cypher, no server, built for this. |
| Local LLM | Ollama, default `qwen2.5:7b` (configurable) | Summaries + JSON-constrained extraction. |
| Extraction | `markitdown` | Many formats → markdown in one call. |
| Hashing | `blake3` | Fast content hashing for change detection. |
| CLI | `typer` + `rich` | Type-driven commands, clean output. |
| Config | `pydantic-settings` + TOML | Typed config from file + env. |

All model names are config-driven; never hardcode them in logic.

---

## 4. Monorepo layout

```
katsi/
├── pyproject.toml              # uv workspace root
├── AGENTS.md                   # repo context for the coding agent
├── README.md
├── katsi.toml.example          # sample config
├── packages/
│   ├── core/
│   │   └── katsi_core/
│   │       ├── models.py       # pydantic data models
│   │       ├── config.py       # settings
│   │       ├── store/
│   │       │   ├── vectors.py  # LanceDB adapter
│   │       │   └── graph.py    # Kùzu adapter + schema DDL
│   │       ├── clients/
│   │       │   ├── embed.py    # Ollama embeddings
│   │       │   └── llm.py      # Ollama chat / JSON extraction
│   │       ├── ingest/
│   │       │   ├── extract.py  # markitdown wrapper
│   │       │   ├── chunk.py    # chunker
│   │       │   ├── enrich.py   # summarize-once + entity/relation extraction
│   │       │   └── pipeline.py # orchestrates one file end-to-end
│   │       └── retrieve/
│   │           ├── search.py   # vector + graph fusion
│   │           └── context.py  # assemble ContextBundle (budget-capped)
│   ├── mcp_server/
│   │   └── katsi_mcp/server.py # FastMCP tools wrapping core
│   └── cli/
│       └── katsi_cli/main.py   # typer app
└── tests/
```

---

## 5. Data models

### 5.1 Pydantic (`core/katsi_core/models.py`)

```python
from datetime import datetime
from enum import Enum
from pydantic import BaseModel


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    STALE = "stale"
    ERROR = "error"


class FileRecord(BaseModel):
    id: str                      # blake3(realpath), stable across content changes
    path: str                    # absolute realpath
    name: str
    ext: str
    mime: str
    size_bytes: int
    mtime: float
    content_hash: str            # blake3 of file bytes — drives skip/reindex
    status: IndexStatus = IndexStatus.PENDING
    summary: str | None = None
    last_indexed_at: datetime | None = None
    error: str | None = None


class Chunk(BaseModel):
    id: str                      # f"{file_id}:{ordinal}"
    file_id: str
    ordinal: int
    text: str
    token_count: int


class Extraction(BaseModel):
    """Strict JSON contract the local model must return."""
    summary: str
    entities: list[dict]         # {"name": str, "kind": "person|org|project"}
    topics: list[str]
    references: list[str]        # paths/filenames this file points at, if any


class FileHit(BaseModel):
    file_id: str
    path: str
    summary: str
    score: float
    why: str                     # short relevance/relationship explanation


class ContextBundle(BaseModel):
    query: str
    files: list[FileHit]
    chunks: list[Chunk]          # only the few highest-scoring raw chunks
    relationships: list[str]     # human-readable graph sketch lines
    token_estimate: int
```

### 5.2 Knowledge graph (Kùzu DDL, `store/graph.py`)

```cypher
CREATE NODE TABLE File(id STRING, path STRING, name STRING, ext STRING,
                       summary STRING, mtime DOUBLE, PRIMARY KEY(id));
CREATE NODE TABLE Entity(name STRING, kind STRING, PRIMARY KEY(name));
CREATE NODE TABLE Topic(name STRING, PRIMARY KEY(name));

CREATE REL TABLE MENTIONS(FROM File TO Entity, weight DOUBLE);
CREATE REL TABLE ABOUT(FROM File TO Topic, weight DOUBLE);
CREATE REL TABLE REFERENCES(FROM File TO File);
CREATE REL TABLE DUPLICATE_OF(FROM File TO File, similarity DOUBLE);
```

Keep the ontology deliberately small for v0.1. Resist adding edge types until a query actually needs them — graph noise is the failure mode.

### 5.3 Vector store (LanceDB, `store/vectors.py`)

One table `chunks` with columns: `id`, `file_id`, `ordinal`, `text`, `vector` (float32, dim = embedding model's), `token_count`. Index the `vector` column for ANN search.

---

## 6. MCP interface (the product surface — keep stable)

All tools live in `mcp_server/katsi_mcp/server.py` and call into `core`. The headline tool is `get_context`: it returns a compact bundle the **client's** model synthesizes over. That return-context-not-answer design is the entire token saver — do not have the server call a cloud model.

```python
@mcp.tool()
def index_status() -> IndexStats:
    """Counts by status, last index time, total chunks. For health/debugging."""

@mcp.tool()
def search(query: str, k: int = 8) -> list[FileHit]:
    """Ranked files for a query, each with a one-line 'why relevant'."""

@mcp.tool()
def get_context(query: str, max_tokens: int = 3000) -> ContextBundle:
    """PRIMARY TOOL. Curated, budget-capped context for the client to answer over:
    file summaries + the few most relevant raw chunks + a graph relationship sketch."""

@mcp.tool()
def get_file_summary(file_id: str) -> FileRecord:
    """Cached summary + metadata for one file (no re-read of the file)."""

@mcp.tool()
def related(file_id: str, kinds: list[str] | None = None) -> list[FileHit]:
    """Graph neighbors: shared entities/topics, references, duplicates."""
```

Optional, behind a config flag for fully-local mode:

```python
@mcp.tool()
def answer(query: str) -> str:
    """Local-model synthesis over get_context output. For sensitive trees
    where nothing should leave the machine. Off by default."""
```

---

## 7. Pipelines

### 7.1 Ingest (one file, `ingest/pipeline.py`)

1. Compute `content_hash` (blake3). If a `FileRecord` exists with the same hash and `status == INDEXED`, **skip** — this is the saver.
2. Extract text via `markitdown`.
3. Chunk (target ~512 tokens, ~64 overlap).
4. Embed chunks (`bge-m3`) → upsert into LanceDB.
5. Summarize-once + extract entities/topics/references in **one** local-model call returning the `Extraction` JSON contract (constrained/validated; retry once on parse failure, then mark `ERROR`).
6. Upsert File node + MENTIONS/ABOUT/REFERENCES edges into Kùzu.
7. Optional dedup: cosine on file-level mean embedding; if > threshold, write `DUPLICATE_OF`.
8. Set `status = INDEXED`, `last_indexed_at`.

### 7.2 Retrieval (`retrieve/`)

1. Embed query → LanceDB ANN, top-N chunks.
2. Graph-expand: resolve those chunks' files, pull 1-hop neighbors (shared entities/topics, references).
3. Score fusion: combine vector score + graph proximity (no learned reranker in v0.1).
4. Assemble `ContextBundle` under `max_tokens`: dedup files, include each file's cached summary, attach only the top few raw chunks, render relationship lines (e.g., `report.md —MENTIONS→ Acme; —REFERENCES→ spec.md`).
5. Return.

---

## 8. `AGENTS.md` (commit this so opencode has standing context)

```markdown
# katsi — agent instructions

Local-first MCP server for relational file context. Python 3.12, uv workspace.

## Rules
- Everything cheap and high-frequency runs on LOCAL models via Ollama. The only
  cloud touchpoint is the MCP client synthesizing over our returned context.
- Model names, paths, thresholds come from config — never hardcode.
- `core` has no MCP or CLI imports. `mcp_server` and `cli` depend on `core`, not
  each other.
- Summarize each file exactly once per content hash. Never re-summarize unchanged files.
- Local extraction must return the strict `Extraction` JSON contract; validate,
  retry once, then mark the file ERROR. Never let a bad parse poison the graph.

## Commands
- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format .`
- Run MCP server: `uv run katsi-mcp`
- Run CLI: `uv run katsi --help`

## Definition of done (every task)
- Type hints throughout; passes ruff.
- Unit tests for the unit built; external services (Ollama/LanceDB/Kùzu) faked or
  fixtured, not hit in CI.
- No TODOs left in the happy path.
```

---

## 9. Delegatable task list

Dependency-ordered. Each is sized for one focused opencode session. Hand them over one at a time, in order. Each has a paste-ready prompt — adjust the project name first.

### T0 — Scaffold the workspace
**Touches:** `pyproject.toml`, `packages/*/pyproject.toml`, `AGENTS.md`, `katsi.toml.example`, `core/models.py`, `core/config.py`
**Done when:** `uv sync` succeeds; `uv run pytest` runs (zero tests OK); the three packages import each other per the dependency rule; `models.py` and `config.py` match §5 and §3.
**Prompt:**
> Set up a `uv` workspace named `katsi` with three packages under `packages/`: `core` (importable as `katsi_core`), `mcp_server` (`katsi_mcp`), and `cli` (`katsi_cli`). `mcp_server` and `cli` depend on `core`; `core` depends on neither. Add `pydantic`, `pydantic-settings`, `typer`, `rich`, `ruff`, `pytest`, `lancedb`, `kuzu`, `ollama`, `markitdown`, `blake3`. Create `core/katsi_core/models.py` and `config.py` exactly per the spec sections I'll paste. Add the `AGENTS.md` and a `katsi.toml.example`. Configure ruff and a `katsi`/`katsi-mcp` script entrypoint.

### T1 — Store adapters
**Touches:** `core/store/vectors.py`, `core/store/graph.py`
**Deps:** T0
**Done when:** LanceDB adapter can create/open the `chunks` table, upsert chunks with vectors, and ANN-search returning ids+scores; Kùzu adapter runs the DDL idempotently and exposes upsert-file-node, upsert-edge, and a 1-hop-neighbors query. Tests use temp dirs.
**Prompt:**
> Implement `store/vectors.py` (LanceDB) and `store/graph.py` (Kùzu) per §5.2/§5.3. Vectors: create/open table, upsert `Chunk` rows with embeddings, `search(vector, k)` → list of (id, file_id, score). Graph: idempotent schema init from the DDL, `upsert_file(File)`, `add_edge(...)`, `neighbors(file_id, hops=1)`. Write pytest using temporary directories; no network.

### T2 — Ollama clients
**Touches:** `core/clients/embed.py`, `core/clients/llm.py`
**Deps:** T0
**Done when:** `embed(texts) -> list[vector]` batches against the configured embedding model; `extract(text) -> Extraction` calls the chat model with a strict instruction to return only the `Extraction` JSON, parses+validates, retries once, raises a typed error on second failure. Tests mock the Ollama client.
**Prompt:**
> Implement `clients/embed.py` and `clients/llm.py` wrapping Ollama. `embed(texts)` batches embeddings using the configured model. `extract(text) -> Extraction` prompts the chat model to return ONLY JSON matching the `Extraction` model (no prose, no fences), validates with pydantic, retries once on parse failure, then raises `ExtractionError`. Mock the Ollama client in tests; assert the retry path.

### T3 — Extract + chunk
**Touches:** `core/ingest/extract.py`, `core/ingest/chunk.py`
**Deps:** T0
**Done when:** `extract_text(path) -> str` handles md/txt/code/pdf/docx via markitdown with graceful failure; `chunk(text) -> list[Chunk]` produces ~512-token chunks with ~64 overlap and stable ordinals. Tests cover a markdown and a code fixture.
**Prompt:**
> Implement `ingest/extract.py` (`extract_text(path)` via markitdown, returns "" and logs on unsupported/failed files) and `ingest/chunk.py` (`chunk(file_id, text)` → list of `Chunk`, ~512 tokens, ~64 overlap, deterministic ordinals/ids). Add fixtures + tests.

### T4 — Ingest pipeline
**Touches:** `core/ingest/enrich.py`, `core/ingest/pipeline.py`
**Deps:** T1, T2, T3
**Done when:** `index_file(path)` executes §7.1 end-to-end including the content-hash skip; an unchanged file on a second call performs zero embedding and zero LLM calls (assert via mocks). File node + edges land in the graph; chunks land in vectors.
**Prompt:**
> Implement `ingest/pipeline.py::index_file(path)` per §7.1: hash → skip-if-unchanged → extract → chunk → embed+upsert → single `extract()` call → upsert File node and MENTIONS/ABOUT/REFERENCES edges → set status. `enrich.py` maps `Extraction` to graph writes. Critical test: calling `index_file` twice on an unchanged file makes zero embed/LLM calls the second time.

### T5 — Retrieval + context assembly
**Touches:** `core/retrieve/search.py`, `core/retrieve/context.py`
**Deps:** T1, T2
**Done when:** `search(query, k)` returns fused vector+graph `FileHit`s with a `why` line; `build_context(query, max_tokens)` returns a `ContextBundle` that never exceeds the budget and includes summaries + top chunks + relationship sketch.
**Prompt:**
> Implement `retrieve/search.py::search(query, k)` (embed query → vector ANN → graph 1-hop expand → score fusion → `FileHit`s with a short `why`) and `retrieve/context.py::build_context(query, max_tokens)` per §7.2, strictly budget-capped. Test the budget cap and that graph neighbors surface even when not in the top vector hits.

### T6 — MCP server
**Touches:** `mcp_server/katsi_mcp/server.py`
**Deps:** T4, T5
**Done when:** FastMCP server exposes the five §6 tools (plus gated `answer`) wired to core; `uv run katsi-mcp` starts over stdio; a smoke test lists tools and calls `get_context` against a fixtured store.
**Prompt:**
> Implement the FastMCP server in `mcp_server/katsi_mcp/server.py` exposing `index_status`, `search`, `get_context`, `get_file_summary`, `related`, and a config-gated `answer`, each delegating to `katsi_core`. Stdio transport, `katsi-mcp` entrypoint. Add a smoke test that initializes against a fixtured store and calls `get_context`.

### T7 — CLI
**Touches:** `cli/katsi_cli/main.py`
**Deps:** T4, T5
**Done when:** `katsi index <path>` walks + indexes with a Rich progress bar; `katsi status`, `katsi search <q>`, `katsi ask <q>` (prints the assembled context, and the local answer if the flag is on) all work end-to-end.
**Prompt:**
> Build a Typer CLI in `cli/katsi_cli/main.py`: `index PATH` (recursive walk honoring config include/exclude globs, Rich progress), `status`, `search QUERY`, `ask QUERY` (prints the `ContextBundle`; with `--local` also prints local-model synthesis). This is the demo + dogfood surface — make the output clean.

### T8 — Package & publish
**Touches:** `README.md`, entrypoints, registry metadata
**Deps:** T6, T7
**Done when:** `uvx katsi-mcp` runs from a clean machine; README opens with a copy-paste MCP client config block and a 60-second quickstart; `server.json`/registry metadata prepared for the official MCP community registry.
**Prompt:**
> Finalize packaging for `uvx katsi-mcp`. Write a README whose first screen is (1) one-line value prop, (2) copy-paste MCP client config block, (3) 60-second quickstart. Prepare metadata to publish to the official MCP community registry (it auto-propagates to the GitHub registry). Add ES + EN README variants.

---

## 10. Suggested delegation order & checkpoints

T0 → T1+T2 (parallelizable) → T3 → T4 → T5 → T6+T7 (parallelizable) → T8.

After T4 you can index a real folder and inspect the graph manually — first checkpoint that the cheap path works. After T6 you can wire it into Claude Desktop and feel the token savings — that's your demo moment and the natural cut line for a v0.1 announcement.

---

## 11. Roadmap (post-v0.1, do not build yet)

- **Watcher**: watchdog → debounced queue → `index_file`, with rename/move handling via inode tracking (avoid re-summarizing moved files).
- **Hierarchical rollups**: folder/project summaries so broad questions never touch a file.
- **Agentic retrieval**: LangGraph multi-hop loop, escalating to a richer context build only when local confidence is low.
- **Local reranker**: `bge-reranker-v2-m3` to ship fewer, sharper chunks.
- **Visual explorer**: sigma.js over the Kùzu graph (standalone surface).
- **More modalities**: image captioning / OCR at ingest.
