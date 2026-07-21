# katsi — agent instructions

Local-first MCP server for relational file context. Python 3.12, uv workspace.

## Rules

- Everything cheap and high-frequency runs on LOCAL models via Ollama. The only cloud touchpoint is the MCP client synthesizing over returned context.
- Model names, paths, and thresholds come from config—never hardcode.
- `core` has no MCP or CLI imports. `mcp_server` and `cli` depend on `core`, not each other.
- Summarize each file exactly once per content hash. Never re-summarize unchanged files.
- Local extraction must return the strict `Extraction` JSON contract. Validate, retry once, then mark the file `ERROR`. Never let a bad parse poison the graph.
- Use type hints throughout and pass Ruff checks.
- Add unit tests for each unit built. Fake or fixture external services (Ollama, LanceDB, Kùzu); never hit them in CI.
- Leave no TODOs in the happy path.

## Commands

- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format .`
- Run MCP server: `uv run katsi-mcp`
- Run CLI: `uv run katsi --help`

## Layout

```text
katsi/
├── pyproject.toml              # uv workspace root
├── packages/
│   ├── core/└── katsi_core/   # models, config, store, clients, ingest, retrieve
│   ├── mcp_server/└── katsi_mcp/server.py
│   └── cli/└── katsi_cli/main.py
└── tests/
```

## Definition of done

- Type hints throughout; Ruff passes.
- Unit tests cover the unit built; external services are mocked or fixtured and never hit in CI.
- No TODOs remain in the happy path.
