# mnemo

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
uvx mnemo-mcp
```

That's it for the server. To actually get value, index a folder then wire the
server into your MCP client (next block).

To index + search the indexed tree locally:

```bash
uvx --from mnemo-cli mnemo index ~/my-folder
uvx --from mnemo-cli mnemo ask "what is this project about?"
```

## MCP client config (Claude Desktop)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS; `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "mnemo": {
      "command": "uvx",
      "args": ["mnemo-mcp"]
    }
  }
}
```

### Other clients

- **Cursor**: Settings → MCP → Add MCP → `uvx mnemo-mcp`.
- **Generic MCP**: any client that speaks MCP stdio can launch `uvx mnemo-mcp`.

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
mnemo index ./some-folder      # recursive walk with include/exclude globs + Rich progress
mnemo status                    # counts + last index time
mnemo search "machine learning" # ranked files
mnemo ask "what is this project about?"   # prints the curated context bundle
mnemo ask "what is this about?" --local   # also runs local-model synthesis (off by default)
```

## Config

A `mnemo.toml` (or `~/.mnemo/mnemo.toml`) is optional. Every field has a default.
See `mnemo.toml.example` for the full schema. Key fields:

```toml
[mnemo.ollama]
host = "http://localhost:11434"
embed_model = "bge-m3"              # multilingual ES/EN/ZH
llm_model = "qwen2.5:7b"

[mnemo.ingest]
chunk_token_target = 512
chunk_token_overlap = 64

[mnemo.retrieve]
top_k_chunks = 16
top_k_files = 8
default_context_max_tokens = 3000

[mnemo.mcp]
enable_answer_tool = false          # local-only synthesis, off by default
```

## Architecture

```
mnemo/
├── packages/
│   ├── core/mnemo_core/   models, config, store, clients, ingest, retrieve
│   ├── mcp_server/        FastMCP tools (this package is what `mnemo-mcp` runs)
│   └── cli/               `mnemo` CLI: index, status, search, ask
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

# AgenticFile
