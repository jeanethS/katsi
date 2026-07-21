# T7 — Typer CLI

Extends the existing katsi workspace. T0–T6 already done — add only the new files.

## TOOL RULES (read first)

Do NOT explore any codebase. Do NOT search for anything.
Use ONLY your file-write/edit tool and `bash` (for `uv run pytest`, `uv run ruff check .`).

When done run `uv run pytest -q 2>&1 | tail -20` and `uv run ruff check . 2>&1 | tail -10`
from the project root, report exit codes + tail outputs.

## 0. Verified Typer API (typer 0.26.x, installed)

```python
from __future__ import annotations
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from pathlib import Path

app = typer.Typer(help="katsi: relational file context", no_args_is_help=True)
console = Console()

@app.command()
def index(path: Path) -> None:
    """Index PATH recursively."""
    # path is auto-coerced from CLI arg.
    ...

@app.command()
def status() -> None:
    """Show indexing status."""
    ...

if __name__ == "__main__":
    app()
```

Console script entrypoint `main()` MUST call `app()` (not `app.run()`) since Typer
supplies `__call__` on the Typer instance.

## 1. What you wire together

From `katsi_core.models`: `ContextBundle`, `FileHit`, `FileRecord`, `IndexStatus`.
From `katsi_core.config`: `Settings`, `get_settings`.
From `katsi_core.ingest.pipeline`: `IngestPipeline`.
From `katsi_core.ingest.records`: `FileRecordStore`.
From `katsi_core.clients.embed`: `EmbedClient`.
From `katsi_core.clients.llm`: `LLMClient`.
From `katsi_core.retrieve.search`: `search`.
From `katsi_core.retrieve.context`: `build_context`.
From `katsi_core.store.graph`: `GraphStore`.
From `katsi_core.store.vectors`: `VectorStore`.
Stdlib: `fnmatch`, `pathlib.Path`, `pathlib.PurePath.is_relative_to` (3.9+)

## 2. Files to create / update (3 files)

```
packages/cli/katsi_cli/main.py     (REWRITE the T0 stub)
packages/cli/katsi_cli/__init__.py  (replace stub; expose main)
tests/test_cli.py                   (NEW)
```

Do NOT touch any other files (T0–T6 stay untouched).

## 3. Contract: `packages/cli/katsi_cli/main.py`

```python
"""katsi CLI: index, status, search, ask."""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn,
                            TaskProgressColumn, TextColumn)
from rich.table import Table

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import LLMClient
from katsi_core.config import Settings, get_settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.retrieve.context import build_context
from katsi_core.retrieve.search import search
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

logger = logging.getLogger(__name__)

app = typer.Typer(help="katsi: local-first relational file context.",
                  no_args_is_help=True)
console = Console()


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
    _state["pipeline"] = IngestPipeline(
        s, graph=_state["graph"], vectors=_state["vectors"],
        embed=_state["embed"], llm=_state["llm"], records=_state["records"],
    )
    return _state


def _matches_any(path_str: str, patterns: list[str]) -> bool:
    p = path_str.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatch(p, pat):
            return True
        # also check basename
        base = p.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(base, pat):
            return True
    return False


def _walk_files(root: Path, include: list[str], exclude: list[str]) -> list[Path]:
    """Yield files under root matching include globs and NOT matching exclude globs."""
    out: list[Path] = []
    if not root.exists():
        return out
    if root.is_file():
        rp = str(root)
        if _matches_any(rp, include) and not _matches_any(rp, exclude):
            out.append(root)
        return out
    for p in root.rglob("*"):
        if p.is_dir():
            continue
        rp = str(p)
        if _matches_any(rp, include):
            if not _matches_any(rp, exclude):
                out.append(p)
    return out


@app.command()
def index(
    path: Path = typer.Argument(..., help="File or directory to index."),
) -> None:
    """Index PATH recursively, honoring include/exclude globs from config."""
    svc = _services()
    s = svc["settings"]
    if not path.exists():
        console.print(f"[red]error:[/] path not found: {path}")
        raise typer.Exit(code=1)
    files = _walk_files(path, s.ingest.include_globs, s.ingest.exclude_globs)
    console.print(f"[bold]indexing[/] {len(files)} file(s) under {path}")
    pipeline = svc["pipeline"]
    counts = {"indexed": 0, "skipped": 0, "error": 0, "stale": 0}
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]indexing", total=len(files) or None)
        for f in files:
            progress.update(task, description=str(f.name)[:40])
            try:
                rec = pipeline.index_file(f)
            except Exception as e:
                logger.warning("index %s failed: %r", f, e)
                counts["error"] += 1
            else:
                if rec.status.value == "indexed":
                    # Distinguish "freshly indexed" vs "skipped (unchanged)".
                    # The pipeline returns INDEXED for both — we don't know which,
                    # so count as "indexed". (T4 could be extended to track this.)
                    counts["indexed"] += 1
                elif rec.status.value == "error":
                    counts["error"] += 1
                elif rec.status.value == "stale":
                    counts["stale"] += 1
            progress.update(task, advance=1)
    table = Table(title="Index summary")
    table.add_column("status")
    table.add_column("count", justify="right")
    for k, v in counts.items():
        table.add_row(k, str(v))
    console.print(table)


@app.command()
def status() -> None:
    """Show indexing status."""
    svc = _services()
    counts = svc["records"].count_by_status()
    total_files = sum(counts.values())
    last_indexed = None
    for rec in svc["records"].list_all():
        if rec.last_indexed_at is not None and (
            last_indexed is None or rec.last_indexed_at > last_indexed
        ):
            last_indexed = rec.last_indexed_at
    try:
        total_chunks = svc["vectors"].count()
    except Exception as e:
        logger.warning("status: vector store count failed: %r", e)
        total_chunks = 0
    table = Table(title="katsi status")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total files", str(total_files))
    for k, v in sorted(counts.items()):
        table.add_row(f"  {k}", str(v))
    table.add_row("total chunks", str(total_chunks))
    table.add_row("last indexed",
                  last_indexed.isoformat() if last_indexed else "(none)")
    table.add_row("data_dir", str(svc["settings"].store.data_dir))
    table.add_row("ollama", f"{svc['settings'].ollama.host} "
                              f"(embed={svc['settings'].ollama.embed_model} "
                              f"llm={svc['settings'].ollama.llm_model})")
    console.print(table)


@app.command()
def search_cmd(
    query: str = typer.Argument(..., help="Search query."),
    k: int = typer.Option(8, "--top", "-k", help="Top-k files to return."),
) -> None:
    """Search the indexed files for QUERY. (NOTE: tool exposed as 'search' to CLI users.)"""
    svc = _services()
    # Name the function search_cmd; Typer uses the function's name as the command name
    # by default. To expose it as `search`, set name="search" on @app.command.
    hits = search(query, k=k, settings=svc["settings"], vectors=svc["vectors"],
                  graph=svc["graph"], embed=svc["embed"], records=svc["records"])
    if not hits:
        console.print("[yellow]no matches[/]")
        return
    table = Table(title=f"search: {query}")
    table.add_column("score", justify="right")
    table.add_column("path")
    table.add_column("why")
    table.add_column("summary")
    for h in hits:
        table.add_row(f"{h.score:.3f}", h.path, h.why,
                      (h.summary or "")[:80])
    console.print(table)

# Force the command name to "search", not "search_cmd":
search_cmd.__name__ = "search"
app.command(name="search")(search_cmd)


@app.command(name="ask")
def ask(
    query: str = typer.Argument(..., help="Question to ask of your indexed files."),
    max_tokens: int = typer.Option(3000, "--max-tokens", help="Token budget for context."),
    local: bool = typer.Option(False, "--local", help="Also run local LLM synthesis."),
) -> None:
    """Print the curated context bundle for QUERY (and optionally a local answer)."""
    svc = _services()
    bundle = build_context(query, max_tokens=max_tokens, settings=svc["settings"],
                            vectors=svc["vectors"], graph=svc["graph"],
                            embed=svc["embed"], records=svc["records"])
    console.print(f"[bold]query:[/] {bundle.query}  "
                  f"[dim]tokens~=[/] {bundle.token_estimate}")
    if not bundle.files:
        console.print("[yellow]no matching files[/]")
        return
    file_table = Table(title="files")
    file_table.add_column("score", justify="right")
    file_table.add_column("path")
    file_table.add_column("why")
    file_table.add_column("summary")
    for h in bundle.files:
        file_table.add_row(f"{h.score:.3f}", h.path, h.why,
                            (h.summary or "")[:80])
    console.print(file_table)
    if bundle.chunks:
        console.print("[bold]top chunks:[/]")
        for c in bundle.chunks:
            console.print(f"[dim]--- {c.id} ({c.token_count} tok) ---[/]")
            console.print(c.text[:400])
    if bundle.relationships:
        console.print("[bold]relationships:[/]")
        for r in bundle.relationships:
            console.print(f"  {r}")
    if local:
        console.print()
        console.print("[bold]local synthesis:[/]")
        # Reuse the server's answer path by building the same prompt.
        from katsi_mcp.server import answer as _answer_tool
        try:
            out = _answer_tool(query)
            console.print(out)
        except PermissionError:
            console.print("[red]answer tool is disabled; "
                          "set katsi.mcp.enable_answer_tool=true to enable.[/]")
        except Exception as e:
            console.print(f"[red]local synthesis failed:[/] {e!r}")


def main() -> None:
    """Entry point: `katsi` console script."""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app()
```

Use this implementation reference VERBATIM.

Key correctness points:
- The `search` command's function is named `search_cmd`, but the command name is
  forced to "search" via the `app.command(name="search")(search_cmd)` line at the
  end of the function definition. (Typer registers the command name from the
  function name by default; we override.)
- The pipeline uses lazy store construction via `_services()` — same pattern as the
  MCP server.

## 4. Contract: `packages/cli/katsi_cli/__init__.py`

```python
"""katsi CLI package."""

__version__ = "0.1.0"


def main() -> None:
    """Entrypoint for `katsi` script."""
    from katsi_cli.main import main as _real
    _real()
```

The deferred import is so importing katsi_cli (e.g. for introspection) does not pull
typer / rich eagerly; they load when `katsi ...` is invoked.

## 5. Contract: `tests/test_cli.py`

Use Typer's CliRunner to test command dispatch; avoid hitting Ollama by injecting
fakes via the module's `_state`.

```python
"""Tests for the katsi Typer CLI."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from katsi_core.clients.embed import EmbedClient
from katsi_core.config import Settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


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
        from katsi_core.models import Extraction
        return Extraction(**_json.loads(self.json_str))
    def chat(self, prompt, *, temperature: float = 0.2):
        return f"local-answer (prompt-len={len(prompt)})"
    def _chat(self, system_prompt, user_text):
        return ""


EXTRACTION_JSON = '{"summary":"doc summary","entities":[{"name":"Acme","kind":"org"}],"topics":["ai"],"references":[]}'


@pytest.fixture
def cli_runner(tmp_path):
    """Return (runner, services dict) wired to tmp_path."""
    from katsi_cli import main as cli_main
    # Build local stores pointing at tmp_path
    s = Settings()
    s.store.data_dir = tmp_path / "katsi_data"
    vectors = VectorStore(tmp_path / "katsi_data" / "vectors")
    vectors.init_table(8)
    graph = GraphStore(tmp_path / "katsi_data" / "graph")
    records = FileRecordStore(tmp_path / "katsi_data" / "records")
    embed = _FakeEmbed(dim=8)
    llm = _FakeLLM(EXTRACTION_JSON)
    pipeline = IngestPipeline(s, graph=graph, vectors=vectors, embed=embed,
                              llm=llm, records=records)

    cli_main._state.clear()
    cli_main._state.update({
        "settings": s, "embed": embed, "llm": llm, "graph": graph,
        "vectors": vectors, "records": records, "pipeline": pipeline,
    })
    from typer.testing import CliRunner as _CR
    return _CR(), cli_main, embed, llm, records, s


def test_status_zero_when_empty(cli_runner):
    runner, cli_main, _, _, _, _ = cli_runner
    res = runner.invoke(cli_main.app, ["status"])
    assert res.exit_code == 0, res.output
    assert "total files" in res.output.lower()


def test_index_processes_md_file(cli_runner, tmp_path):
    runner, cli_main, embed, llm, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Hello\n\nmentions Acme.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    assert embed.calls == 1
    assert llm.calls == 1


def test_search_prints_results_after_indexing(cli_runner, tmp_path):
    runner, cli_main, embed, _, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme Doc\n\nMentions Acme and AI.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    res2 = runner.invoke(cli_main.app, ["search", "Acme AI"])
    assert res2.exit_code == 0, res2.output
    assert "doc.md" in res2.output


def test_ask_prints_bundle_and_relationships(cli_runner, tmp_path):
    runner, cli_main, _, _, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme\n\nMentions Acme and AI.")
    res = runner.invoke(cli_main.app, ["index", str(md_path)])
    assert res.exit_code == 0, res.output
    res2 = runner.invoke(cli_main.app, ["ask", "Acme AI"])
    assert res2.exit_code == 0, res2.output
    assert "doc.md" in res2.output
    # at least the file path should appear since bundle contains the file
    assert "score" in res2.output.lower() or "matches" in res2.output.lower() or "doc.md" in res2.output


def test_index_missing_path_errors(cli_runner, tmp_path):
    runner, cli_main, _, _, _, _ = cli_runner
    nonsense = tmp_path / "does_not_exist"
    res = runner.invoke(cli_main.app, ["index", str(nonsense)])
    assert res.exit_code != 0


def test_ask_local_with_disabled_answer_tool_passes(cli_runner, tmp_path):
    """The --local flag should not crash if the answer tool is disabled.
       The CLI gracefully prints a 'disabled' note."""
    runner, cli_main, _, _, _, _ = cli_runner
    md_path = tmp_path / "doc.md"
    md_path.write_text("# Acme\n\nMentions Acme and AI.")
    runner.invoke(cli_main.app, ["index", str(md_path)])
    res = runner.invoke(cli_main.app, ["ask", "Acme AI", "--local"])
    # The bundle print should always succeed even if local synthesis is disabled.
    assert res.exit_code == 0, res.output
    assert "doc.md" in res2.output if False else True   # noqa: F841
    # We can't rely on the local-synth text since enable_answer_tool defaults False.
    # The only requirement: exit_code 0 + path present in bundle.
    assert "doc.md" in res.output


def test_help_lists_all_four_commands(cli_runner):
    runner, cli_main, _, _, _, _ = cli_runner
    res = runner.invoke(cli_main.app, ["--help"])
    assert res.exit_code == 0, res.output
    assert "index" in res.output
    assert "status" in res.output
    assert "search" in res.output
    assert "ask" in res.output
```

Note: the last test `test_help_lists_all_four_commands` must show all four commands
exposed via `katsi --help`. Your `app = typer.Typer(no_args_is_help=True)` exposes
the help on missing args.

## 6. Constraints

- Do NOT add new dependencies. typer + rich are already in katsi-cli deps.
- Do NOT modify any T0–T6 files except the `packages/cli/katsi_cli/__init__.py`
  stub (which only had a docstring + NotImplementedError).
- Do NOT leave TODO comments.
- Do NOT actually call Ollama in tests (use fakes).
- The `--local` flag on `ask` should NOT crash when the answer tool is disabled —
  the CLI should print a friendly note and exit 0.

## 7. Done when

- All 3 files exist with the contracts above.
- `uv run pytest` passes (existing ~71 + ~7 cli = ~78+).
- `uv run ruff check .` is clean.
- `uv run katsi --help` lists commands: index, status, search, ask.
- Hand back a short report.
