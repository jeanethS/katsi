# T8 — Package & publish

Final task. Extends the existing katsi workspace. T0–T7 already done. This task
polishes packaging + docs so `uvx katsi-mcp` works from a clean machine and the
README opens with a copy-paste MCP client config block + 60-second quickstart.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv build` etc.).
You MAY modify `README.md` (already exists from T0), `pyproject.toml`, and the
per-package `pyproject.toml` files ONLY for adding metadata fields (license,
authors, urls). Do NOT change existing dependencies.

When done run `uv build 2>&1 | tail -30` and `uv run pytest -q 2>&1 | tail -5`
and `uv run ruff check . 2>&1 | tail -5`, paste tail outputs.

## 1. Files to CREATE / UPDATE (6 files)

### CREATE
```
README.es.md                       Spanish README variant
packages/mcp_server/server.json     MCP community registry metadata
LICENSE                             MIT license file
```

### UPDATE
```
README.md                           (rewrite per §3 below)
pyproject.toml                      (only add metadata — license-classifiers, urls)
packages/mcp_server/pyproject.toml  (only add metadata)
packages/cli/pyproject.toml         (only add metadata)
packages/core/pyproject.toml        (only add metadata)
```

## 2. Copy-paste MCP client config block

This block is the headline of the README. It is what every MCP client wants:

```json
{
  "mcpServers": {
    "katsi": {
      "command": "uvx",
      "args": ["katsi-mcp"]
    }
  }
}
```

Some clients need slightly different syntax (e.g. Cursor needs the `args` to be
`["--from", "katsi-mcp", "katsi-mcp"]`). The README should give the canonical
Claude Desktop config first; underneath cover Claude Desktop, Cursor, and
generic MCP clients briefly.

## 3. README.md rewrite (exact structure)

```markdown
# katsi

Local-first, privacy-first MCP server that gives any MCP client (Claude Desktop,
Code, Cursor, ...) cheap, **relational** context about your files. Summarize each
file exactly once with local Ollama models, bank into a vector store + knowledge
graph (Kùzu), and at query time return a small curated context bundle so your
client's model synthesizes over a tiny window instead of exploring the filesystem.

> Exploration tokens move from query-time (expensive, cloud, repeated) to
> ingest-time (cheap, local, once). The only cloud spend is the client
> synthesizing the answer over a small curated context.

## What makes it different

- **Summarize-once** per file with a local model. Cached. Re-summarized only when
  the file's content-hash changes — never on unchanged files.
- **Relational, not just vector.** Local entities + topics + references land in an
  embedded Kùzu graph; retrieval fuses vector similarity with graph neighbors so
  files connected via shared entities / topics / cross-references surface even when
  not in the top-N vector hits.
- **Local-first.** Summaries, embeddings, entity extraction, and graph queries all
  run on Ollama + LanceDB + Kùzu locally. Nothing leaves your machine.
- **Returns context, not answers.** The MCP server does NOT call a cloud model —
  it returns a `ContextBundle` of file summaries + top raw chunks + a relationship
  sketch for the *client's* model to synthesize over.

## Quickstart (60 seconds)

```bash
# 1. Install + run (one line)
uvx katsi-mcp
```

That's it for the server. To actually get value, index a folder then wire the
server into your MCP client (next block).

To index + search the indexed tree locally:

```bash
uvx --from katsi-cli katsi index ~/my-folder
uvx --from katsi-cli katsi ask "what is this project about?"
```

## MCP client config (Claude Desktop)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS; `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "katsi": {
      "command": "uvx",
      "args": ["katsi-mcp"]
    }
  }
}
```

### Other clients

- **Cursor**: Settings → MCP → Add MCP → `uvx katsi-mcp`.
- **Generic MCP**: any client that speaks MCP stdio can launch `uvx katsi-mcp`.

## Provided MCP tools

| Tool | Purpose |
|---|---|
| `get_context(query, max_tokens=3000)` | **PRIMARY** — budget-capped bundle of file summaries + top raw chunks + graph relationship sketch for the client to answer over. |
| `search_files(query, k=8)` | Ranked files for a query with a one-line "why relevant". |
| `related(file_id, kinds?)` | Graph neighbors (shared entities / topics / references / duplicates). |
| `get_file_summary(file_id)` | Cached summary + metadata for a file. |
| `index_status()` | Counts by status, last index time, chunk counts. |
| `index_file_tool(path)` | Trigger one-file ingestion from the client. |
| `answer(query)` | *(off by default)* Fully-local synthesis over the bundle — never leaves the machine. Enable with `enable_answer_tool=true`. |

## CLI dogfood surface

```bash
katsi index ./some-folder      # recursive walk with include/exclude globs + Rich progress
katsi status                    # counts + last index time
katsi search "machine learning" # ranked files
katsi ask "what is this project about?"   # prints the curated context bundle
katsi ask "what is this about?" --local   # also runs local-model synthesis (off by default)
```

## Config

A `katsi.toml` (or `~/.katsi/katsi.toml`) is optional. Every field has a default.
See `katsi.toml.example` for the full schema. Key fields:

```toml
[katsi.ollama]
host = "http://localhost:11434"
embed_model = "bge-m3"          # multilingual ES/EN/ZH
llm_model = "qwen2.5:7b"

[katsi.ingest]
chunk_token_target = 512
chunk_token_overlap = 64

[katsi.retrieve]
top_k_chunks = 16
top_k_files = 8
default_context_max_tokens = 3000

[katsi.mcp]
enable_answer_tool = false     # local-only synthesis, off by default
```

## Architecture

```
katsi/
├── packages/
│   ├── core/katsi_core/   models, config, store, clients, ingest, retrieve
│   ├── mcp_server/        FastMCP tools (this package is what `katsi-mcp` runs)
│   └── cli/               `katsi` CLI: index, status, search, ask
└── tests/
```

Local stack:
- **Embeddings**: Ollama `bge-m3` (local, multilingual).
- **Vector store**: LanceDB (embedded, on-disk).
- **Graph store**: Kùzu (embedded, Cypher).
- **LLM**: Ollama `qwen2.5:7b` (configurable) — summaries + JSON-constrained extraction.
- **Extraction**: `markitdown` for md/txt/code/pdf/docx → markdown.

## Status

v0.1 (initial release). The roadmap post-v0.1: file watcher (live re-index),
folder/project hierarchical rollups, agentic multi-hop retrieval via LangGraph,
local reranker (`bge-reranker-v2-m3`), visual graph explorer.

## License

MIT — see [LICENSE](LICENSE).
```

## 4. README.es.md (Spanish variant) — same structure, translated

Provide an equivalent Spanish README. No new content; mirror the English version
section-for-section. Use natural Spanish phrasing. Code blocks (JSON/CLI commands)
are identical (with minimal commentary in Spanish).

## 5. LICENSE (MIT)

Standard MIT license, copyright year = current year (2026), holder = "katsi contributors".

## 6. server.json (MCP community registry metadata)

Per the official MCP community registry schema (resembles a PyPI/registry entry:

```json
{
  "$schema": "https://raw.githubusercontent.com/modelcontextprotocol/registry/main/schema/registry.schema.json",
  "name": "katsi",
  "description": "Local-first MCP server that gives any MCP client cheap, relational context about a user's files. Summarize-once + knowledge graph + vector store; returns a budget-capped context bundle for the client to answer over.",
  "repository": "https://github.com/JEANETH_USER/katsi",
  "publisher": "katsi",
  "version": "0.1.0",
  "homepage": "https://github.com/JEANETH_USER/katsi",
  "license": "MIT",
  "keywords": ["mcp", "rag", "knowledge-graph", "ollama", "local", "privacy"],
  "categories": ["local", "search", "files"],
  "author": {
    "name": "katsi contributors"
  },
  "bin": "katsi-mcp",
  "runtime": "python",
  "install": {
    "type": "uvx",
    "command": "uvx",
    "args": ["katsi-mcp"]
  },
  "tools": [
    {"name": "get_context", "description": "Curated, budget-capped context bundle for the client to answer over."},
    {"name": "search_files", "description": "Ranked files for a query, each with a one-line 'why relevant'."},
    {"name": "related", "description": "Graph neighbors: shared entities/topics, references, duplicates."},
    {"name": "get_file_summary", "description": "Cached summary + metadata for one file."},
    {"name": "index_status", "description": "Counts by status, last index time, total chunks."},
    {"name": "index_file_tool", "description": "Trigger one-file ingestion from the client."},
    {"name": "answer", "description": "(off by default) Local-model synthesis over the bundle."}
  ]
}
```

Replace `JEANETH_USER` placeholder in the `repository` + `homepage` URL with the
public GitHub username. Use `jeaneths` (case-insensitive: lowercase GitHub
usernames). So `https://github.com/jeanethS/katsi`.

## 7. pyproject.toml updates (root + 3 packages)

Add these metadata blocks to each `[project]` table. Do NOT change dependencies
or remove existing fields.

Root `pyproject.toml`:
```toml
authors = [{ name = "katsi contributors" }]
license = { text = "MIT" }
readme = "README.md"
keywords = ["mcp", "rag", "knowledge-graph", "ollama", "local", "privacy"]
classifiers = [
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "License :: OSI Approved :: MIT License",
  "Operating System :: OS Independent",
  "Topic :: Scientific/Engineering :: Artificial Intelligence",
]

[project.urls]
Homepage = "https://github.com/jeanethS/katsi"
Repository = "https://github.com/jeanethS/katsi"
Issues = "https://github.com/jeanethS/katsi/issues"
```

For each per-package pyproject.toml (core, mcp_server, cli): add the SAME metadata
block (same URLs + classifiers + license + readme). This makes each package
self-contained for publishing.

Be careful: existing entries `requires-python = ">=3.12,<3.14"` and `[project.scripts]`
must be preserved exactly.

## 8. uvx verification

After updating, run `uv build --package katsi-mcp 2>&1 | tail -30` to verify the
mcp_server package builds cleanly. Do NOT push to PyPI. The success criterion is
that `uv build` produces a wheel/sdist without errors.

Do NOT run `uvx katsi-mcp` from this machine — to keep this task offline-safe,
just verify the build succeeds. The README's quickstart hypothesis (that
`uvx katsi-mcp` works) is implied by `uv build` publishing capability.

## 9. Constraints

- Do NOT add new dependencies. The deps from T0 stand.
- Do NOT change `requires-python` or `[project.scripts]`.
- Do NOT modify any `.py` source file from T0–T7. Only metadata in pyproject.toml
  files + the doc files.
- Do NOT leave TODO comments.
- Do NOT actually publish to PyPI.

## 10. Done when

- 3 new files exist: `README.es.md`, `packages/mcp_server/server.json`, `LICENSE`.
- `README.md` rewritten per §3.
- All 4 pyproject.toml files have the new metadata fields added.
- `uv build --package katsi-mcp` exits 0 — paste tail output.
- `uv run pytest -q` exits 0 — paste tail (should still be 78+ tests passing).
- `uv run ruff check .` exits 0 — paste tail.
- Hand back a short report with file list and tail outputs.
