"""mnemo CLI: index, status, search, ask."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from mnemo_core.clients.embed import EmbedClient
from mnemo_core.clients.llm import LLMClient
from mnemo_core.config import get_settings
from mnemo_core.ingest.pipeline import IngestPipeline
from mnemo_core.ingest.records import FileRecordStore
from mnemo_core.retrieve.context import build_context
from mnemo_core.retrieve.search import search
from mnemo_core.store.graph import GraphStore
from mnemo_core.store.vectors import VectorStore
from mnemo_core.synth import SynthConfigError, build_synthesizer

logger = logging.getLogger(__name__)

app = typer.Typer(help="mnemo: local-first relational file context.", no_args_is_help=True)
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
        s,
        graph=_state["graph"],
        vectors=_state["vectors"],
        embed=_state["embed"],
        llm=_state["llm"],
        records=_state["records"],
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
        if _matches_any(rp, include) and not _matches_any(rp, exclude):
            out.append(p)
    return out


@app.command()
def index(
    path: Path = typer.Argument(..., help="File or directory to index."),  # noqa: B008
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
    table = Table(title="mnemo status")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total files", str(total_files))
    for k, v in sorted(counts.items()):
        table.add_row(f"  {k}", str(v))
    table.add_row("total chunks", str(total_chunks))
    table.add_row("last indexed", last_indexed.isoformat() if last_indexed else "(none)")
    table.add_row("data_dir", str(svc["settings"].store.data_dir))
    table.add_row(
        "ollama",
        f"{svc['settings'].ollama.host} "
        f"(embed={svc['settings'].ollama.embed_model} "
        f"llm={svc['settings'].ollama.llm_model})",
    )
    console.print(table)


@app.command()
def search_cmd(
    query: str = typer.Argument(..., help="Search query."),
    k: int = typer.Option(8, "--top", "-k", help="Top-k files to return."),  # noqa: B008
) -> None:
    """Search the indexed files for QUERY. (NOTE: tool exposed as 'search' to CLI users.)"""
    svc = _services()
    # Name the function search_cmd; Typer uses the function's name as the command name
    # by default. To expose it as `search`, set name="search" on @app.command.
    hits = search(
        query,
        k=k,
        settings=svc["settings"],
        vectors=svc["vectors"],
        graph=svc["graph"],
        embed=svc["embed"],
        records=svc["records"],
    )
    if not hits:
        console.print("[yellow]no matches[/]")
        return
    table = Table(title=f"search: {query}")
    table.add_column("score", justify="right")
    table.add_column("path")
    table.add_column("why")
    table.add_column("summary")
    for h in hits:
        table.add_row(f"{h.score:.3f}", h.path, h.why, (h.summary or "")[:80])
    console.print(table)


# Force the command name to "search", not "search_cmd":
search_cmd.__name__ = "search"
app.command(name="search")(search_cmd)


@app.command(name="ask")
def ask(
    query: str = typer.Argument(..., help="Question to ask of your indexed files."),
    max_tokens: int = typer.Option(3000, "--max-tokens", help="Token budget for context."),  # noqa: B008
    local: bool = typer.Option(
        False,
        "--local",  # noqa: B008
        help="[deprecated] Use --mode local instead.",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",  # noqa: B008
        help="Synthesis mode: return_only|local|cloud|auto. Defaults to config.",
    ),
) -> None:
    """Print the curated context bundle for QUERY (and optionally synthesize an answer)."""
    svc = _services()
    bundle = build_context(
        query,
        max_tokens=max_tokens,
        settings=svc["settings"],
        vectors=svc["vectors"],
        graph=svc["graph"],
        embed=svc["embed"],
        records=svc["records"],
    )
    console.print(f"[bold]query:[/] {bundle.query}  [dim]tokens~=[/] {bundle.token_estimate}")
    if not bundle.files:
        console.print("[yellow]no matching files[/]")
        return
    file_table = Table(title="files")
    file_table.add_column("score", justify="right")
    file_table.add_column("path")
    file_table.add_column("why")
    file_table.add_column("summary")
    for h in bundle.files:
        file_table.add_row(f"{h.score:.3f}", h.path, h.why, (h.summary or "")[:80])
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
    resolved_mode = mode or ("local" if local else None)
    if resolved_mode:
        console.print()
        try:
            synth = build_synthesizer(svc["settings"], mode=resolved_mode)
            result = synth.answer(query, bundle)
            text_out = result.text or "(none — return_only)"
            console.print(
                f"[bold]synthesis (mode={result.mode}, escalated={result.escalated}):[/] {text_out}"
            )
        except SynthConfigError as e:
            console.print(f"[red]synthesis config error:[/] {e}")
        except PermissionError as e:
            console.print(f"[red]synthesis error:[/] {e}")
        except Exception as e:
            console.print(f"[red]synthesis failed:[/] {e!r}")


def main() -> None:
    """Entry point: `mnemo` console script."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app()
