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
- Type hints throughout; passes ruff.
- Unit tests for the unit built; external services (Ollama/LanceDB/Kùzu) faked or
  fixtured, not hit in CI.
- No TODOs left in the happy path.

## Commands
- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check . && uv run ruff format .`
- Run MCP server: `uv run katsi-mcp`
- Run CLI: `uv run katsi --help`

## Layout
```
katsi/
├── pyproject.toml              # uv workspace root
├── packages/
│   ├── core/└── katsi_core/   # models, config, store, clients, ingest, retrieve
│   ├── mcp_server/└── katsi_mcp/server.py
│   └── cli/└── katsi_cli/main.py
└── tests/
```

## Definition of done (every task)
- Type hints throughout; passes ruff.
- Unit tests for the unit built; external services mocked/fixtured, not hit in CI.
- No TODOs left in the happy path.

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Triage uses the default Matt Pocock label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Domain documentation uses a multi-context layout. See `docs/agents/domain.md`.
