# T0 — Scaffold the katsi uv workspace

You are working inside an EMPTY greenfield project directory `katsi/` at the current
working directory. The only file present is `AGENTS.md` and this spec file (in
`.hermes/`). Everything below must be created from scratch.

## TOOL RULES (read first)

Do NOT explore any codebase.
Do NOT search for anything.
Do NOT call glob, task, doom_loop, or any discovery tool.
Use ONLY your file-write/edit tool and `bash` (for `uv sync`, `uv run pytest`,
`uv run ruff check .`).
Write each file directly with the exact contents specified below.
Do not invent extra files. Do not skip files. Do not paraphrase the spec contents.

When done, run these three commands in order, in the project root, and report their
exit codes and a 400-char tail of each output:
  1. `uv sync`
  2. `uv run pytest`
  3. `uv run ruff check .`

## 1. File tree to create

```
katsi/                              (working dir)
├── AGENTS.md                       (already exists — do not touch)
├── pyproject.toml                  (uv workspace root + dev tools)
├── katsi.toml.example              (sample config)
├── README.md                       (one-screen overview; quickstart placeholder)
├── .gitignore
├── packages/
│   ├── core/
│   │   ├── pyproject.toml
│   │   └── katsi_core/
│   │       ├── __init__.py
│   │       ├── models.py
│   │       └── config.py
│   ├── mcp_server/
│   │   ├── pyproject.toml
│   │   └── katsi_mcp/
│   │       └── __init__.py
│   └── cli/
│       ├── pyproject.toml
│       └── katsi_cli/
│           └── __init__.py
└── tests/
    ├── __init__.py
    └── test_smoke.py
```

That's 14 files. Create all of them with the exact contents in §3 below.

## 2. Dependency rule (critical)

- `katsi_core` depends on NOTHING in this repo (no mcp_server, no cli imports).
- `katsi_mcp` depends on `katsi-core` workspace package.
- `katsi_cli` depends on `katsi-core` workspace package.
- `katsi_mcp` and `katsi_cli` do NOT depend on each other.
- `uv` workspace members are `packages/*`.

## 3. Exact file contents

### 3.1 `pyproject.toml` (root — uv workspace + dev tooling)

```toml
[project]
name = "katsi"
version = "0.1.0"
description = "Local-first MCP server for relational file context."
requires-python = ">=3.12,<3.14"

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
katsi-core = { workspace = true }
katsi-mcp = { workspace = true }
katsi-cli = { workspace = true }

[tool.uv]
dev-dependencies = [
    "ruff>=0.6",
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[tool.ruff]
line-length = 100
target-version = "py312"
extend-exclude = [".venv", "build", "dist"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "N"]
ignore = ["E501"]   # let line-length be advisory; formatter enforces soft wrap

[tool.ruff.lint.isort]
known-first-party = ["katsi_core", "katsi_mcp", "katsi_cli"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-ra"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = []
```

### 3.2 `packages/core/pyproject.toml`

```toml
[project]
name = "katsi-core"
version = "0.1.0"
description = "katsi core: models, config, stores, ingest, retrieve."
requires-python = ">=3.12,<3.14"
dependencies = [
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "lancedb>=0.6",
    "kuzu>=0.4",
    "ollama>=0.3",
    "markitdown>=0.0.1",
    "blake3>=0.3",
    "typer>=0.12",
    "rich>=13.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["katsi_core"]
```

### 3.3 `packages/mcp_server/pyproject.toml`

```toml
[project]
name = "katsi-mcp"
version = "0.1.0"
description = "katsi MCP server (FastMCP)."
requires-python = ">=3.12,<3.14"
dependencies = [
    "katsi-core",
    "mcp>=1.0",
]

[project.scripts]
katsi-mcp = "katsi_mcp.server:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["katsi_mcp"]
```

### 3.4 `packages/cli/pyproject.toml`

```toml
[project]
name = "katsi-cli"
version = "0.1.0"
description = "katsi CLI: index, status, search, ask."
requires-python = ">=3.12,<3.14"
dependencies = [
    "katsi-core",
    "typer>=0.12",
    "rich>=13.7",
]

[project.scripts]
katsi = "katsi_cli.main:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["katsi_cli"]
```

### 3.5 `packages/core/katsi_core/__init__.py`

```python
"""katsi core package."""

__version__ = "0.1.0"
```

### 3.6 `packages/core/katsi_core/models.py` — EXACTLY this content

```python
"""katsi data models.

Strictly follows §5.1 of the architecture spec. Do not rename fields, do not
add defaults beyond what is specified.
"""

from __future__ import annotations

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

### 3.7 `packages/core/katsi_core/config.py` — EXACTLY this content

```python
"""katsi configuration.

Loads from katsi.toml (TOML file) with env var overrides.
Never hardcode model names / paths / thresholds — read from this Settings object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import tomllib
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OllamaSettings(BaseModel):
    host: str = "http://localhost:11434"
    embed_model: str = "bge-m3"
    llm_model: str = "qwen2.5:7b"
    timeout: float = 120.0


class StoreSettings(BaseModel):
    data_dir: Path = Path.home() / ".katsi"
    lancedb_table: str = "chunks"
    kuzu_db: str = "graph"


class IngestSettings(BaseModel):
    chunk_token_target: int = 512
    chunk_token_overlap: int = 64
    dedup_similarity_threshold: float = 0.92
    include_globs: list[str] = Field(
        default_factory=lambda: ["**/*.md", "**/*.txt", "**/*.py",
                                  "**/*.ts", "**/*.pdf", "**/*.docx"]
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: ["**/.git/**", "**/node_modules/**",
                                  "**/.venv/**", "**/__pycache__/**"]
    )


class RetrieveSettings(BaseModel):
    top_k_chunks: int = 16
    top_k_files: int = 8
    graph_expand_hops: int = 1
    vector_weight: float = 0.6
    graph_weight: float = 0.4
    default_context_max_tokens: int = 3000


class MCPSettings(BaseModel):
    enable_answer_tool: bool = False


class Settings(BaseSettings):
    """Top-level settings. Loaded from katsi.toml if present, env-overridable."""

    model_config = SettingsConfigDict(
        env_prefix="KATSI_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    store: StoreSettings = Field(default_factory=StoreSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    retrieve: RetrieveSettings = Field(default_factory=RetrieveSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> Settings:
        """Load settings from a TOML file or default locations.

        Env vars (KATSI_*) override the file. If no path given, look for
        katsi.toml in CWD then ~/.katsi/katsi.toml.
        """
        env_settings = cls()
        if config_path is None:
            cwd_path = Path.cwd() / "katsi.toml"
            home_path = Path.home() / ".katsi" / "katsi.toml"
            config_path = cwd_path if cwd_path.exists() else (
                home_path if home_path.exists() else None
            )
        if config_path is None or not config_path.exists():
            return env_settings
        with open(config_path, "rb") as f:
            data = tomllib.load(f).get("katsi", {})
        if not data:
            return env_settings
        # Merge file values over env defaults (env already resolved above).
        ovr = {}
        for k, v in env_settings.model_dump().items():
            section = data.get(k, {})
            merged = v.copy()
            merged.update(section)
            ovr[k] = merged
        return cls(**ovr)


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.load()
    return _settings


def reset_settings() -> None:
    """Test helper: clears the cached singleton."""
    global _settings
    _settings = None
```

### 3.8 `packages/mcp_server/katsi_mcp/__init__.py`

```python
"""katsi MCP server package (T0 stub — implemented in T6)."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `katsi-mcp` script. Implemented in T6."""
    raise NotImplementedError("katsi-mcp server is implemented in T6")
```

### 3.9 `packages/cli/katsi_cli/__init__.py`

```python
"""katsi CLI package (T0 stub — implemented in T7)."""

__version__ = "0.1.0"  # noqa: F841
```

### 3.10 `tests/__init__.py`

```python
"""katsi test suite."""
```

### 3.11 `tests/test_smoke.py`

A minimal smoke test verifying imports and basic model construction:

```python
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
    b = ContextBundle(query="q", files=[h], chunks=[], relationships=[],
                      token_estimate=10)
    assert b.files == [h]
```

### 3.12 `katsi.toml.example`

```toml
# katsi configuration example. Copy to katsi.toml or ~/.katsi/katsi.toml.
# Every field below has a builtin default — only set what you want to change.

[katsi.ollama]
host = "http://localhost:11434"
embed_model = "bge-m3"
llm_model = "qwen2.5:7b"
timeout = 120.0

[katsi.store]
data_dir = "~/.katsi"
lancedb_table = "chunks"
kuzu_db = "graph"

[katsi.ingest]
chunk_token_target = 512
chunk_token_overlap = 64
dedup_similarity_threshold = 0.92
include_globs = ["**/*.md", "**/*.txt", "**/*.py", "**/*.ts", "**/*.pdf", "**/*.docx"]
exclude_globs = ["**/.git/**", "**/node_modules/**", "**/.venv/**", "**/__pycache__/**"]

[katsi.retrieve]
top_k_chunks = 16
top_k_files = 8
graph_expand_hops = 1
vector_weight = 0.6
graph_weight = 0.4
default_context_max_tokens = 3000

[katsi.mcp]
enable_answer_tool = false
```

### 3.13 `README.md`

```markdown
# katsi

Local-first, privacy-first MCP server that gives any MCP client (Claude Desktop,
Code, Cursor, ...) relational context about your files. Summarize-once per file
with local models, bank into a knowledge graph + vector store, return a small
curated context bundle at query time.

## Status

v0.1 in development. See `Foldersote-architecture-and-tasks.md` for the spec.

## Quickstart (coming in T8)

```bash
uv sync
uv run katsi index ./some-folder
uv run katsi ask "what is this project about?"
uvx katsi-mcp    # for MCP client config once T6 lands
```
```

### 3.14 `.gitignore`

```gitignore
__pycache__/
*.pyc
.venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
build/
dist/
*.egg-info/
.katsi/
katsi.toml
```

## 4. Success criteria (do not mark complete until all pass)

1. All 14 files created with EXACT contents above.
2. `uv sync` exits 0 from project root.
3. `uv run pytest` exits 0 (6 tests passing).
4. `uv run ruff check .` exits 0 (no lint errors).
5. The three packages import each other per the dependency rule:
   - `python -c "import katsi_core"` works
   - `python -c "import katsi_mcp"` works
   - `python -c "import katsi_cli"` works
6. Run `uv run python -c "import katsi_core; from katsi_core.models import FileRecord; print('OK')"` — prints OK.

## 5. After writing all files

Run, in the project root, and paste exit codes + last 400 chars of each output:

```bash
uv sync && uv run pytest && uv run ruff check .
```

Then run:

```bash
uv run python -c "import katsi_core, katsi_mcp, katsi_cli; from katsi_core.models import FileRecord, Chunk, Extraction, FileHit, ContextBundle, IndexStatus; from katsi_core.config import Settings; s = Settings(); print('OK', s.ollama.embed_model, s.ollama.llm_model)"
```

Report both outputs exactly. Done.
