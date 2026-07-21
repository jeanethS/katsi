# katsi

> The word *katsi* means "to know" or "to understand" in Totonac.

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
- **Returns context, not answers (by default).** The MCP server does NOT call a
  cloud model — it returns a `ContextBundle` of file summaries + top raw chunks +
  a relationship sketch for the *client's* model to synthesize over. Server-side
  synthesis (local, cloud, or auto) is opt-in — see [Synthesis modes](#synthesis-modes).

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
| `answer(query, mode?)` | *(off by default)* Server-side synthesis over the bundle using the configured backend (`return_only`/`local`/`cloud`/`auto`); `mode` overrides per call. Returns the answer plus the mode that ran and whether it escalated. Enable with `enable_answer_tool=true`. |

## CLI dogfood surface

```bash
katsi index ./some-folder      # recursive walk with include/exclude globs + Rich progress
katsi status                    # counts + last index time
katsi search "machine learning" # ranked files
katsi ask "what is this project about?"        # prints the curated context bundle
katsi ask "what is this about?" --mode local   # + local-model (Ollama) synthesis
katsi ask "compare these designs" --mode auto  # local, escalating to cloud if warranted
```

`ask` prints which mode actually ran and whether it escalated. (`--local` still
works but is deprecated in favor of `--mode local`.)

## Config

A `katsi.toml` (or `~/.katsi/katsi.toml`) is optional. Every field has a default.
See `katsi.toml.example` for the full schema. Key fields:

```toml
[katsi.ollama]
host = "http://localhost:11434"
embed_model = "bge-m3"              # multilingual ES/EN/ZH
llm_model = "qwen2.5:7b"

[katsi.ingest]
chunk_token_target = 512
chunk_token_overlap = 64

[katsi.retrieve]
top_k_chunks = 16
top_k_files = 8
default_context_max_tokens = 3000

[katsi.mcp]
enable_answer_tool = false          # server-side synthesis (answer tool), off by default
```

## Synthesis modes

katsi does all retrieval locally. You choose where answers are synthesized:

- **return_only** (default) — katsi returns the curated `ContextBundle`; your
  MCP client's model answers. Zero cloud tokens spent by katsi.
- **local** — a local model (Ollama) writes the answer. $0, private, offline.
- **cloud** — your own API key; katsi sends only a tight context bundle (not
  the whole tree). Anthropic by default, provider-pluggable.
- **auto** — answers locally, escalating to cloud only for cross-document
  questions (file count, token estimate, or intent keywords).

Set `synth.backend` in `katsi.toml`, or override per call:

| Surface | Override |
|---|---|
| MCP `answer` tool | `mode` argument |
| CLI `ask` | `--mode {return_only\|local\|cloud\|auto}` |

Example config:

```toml
[katsi.synth]
backend = "auto"
allow_per_call_override = true

[katsi.synth.local]
model = "qwen2.5:7b"
max_tokens = 800

[katsi.synth.cloud]
provider = "anthropic"
model = "claude-sonnet-4-20250514"
api_key_env = "ANTHROPIC_API_KEY"
enable_prompt_caching = true
max_tokens = 1024

[katsi.synth.auto]
escalate_when_files_gte = 4
escalate_when_tokens_gte = 2500
escalate_on_intents = ["compare", "contrast", "synthesize", "across", "difference"]
fallback_to_local_if_cloud_unavailable = true
```

The default is zero-cloud-cost. Devs opt into cloud per config or per call.

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
