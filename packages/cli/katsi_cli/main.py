"""katsi CLI: index, status, search, ask."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as PathLib
from threading import Event
from uuid import UUID

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import LLMClient
from katsi_core.config import get_settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.ingest.walk import matches_any, walk_files
from katsi_core.media.contracts import MediaRepresentationKind
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.media.reprocess import MediaReprocessor, ReprocessCounts, _duration_ms
from katsi_core.media.vision_caption import VisionCaptioner, caption_video
from katsi_core.retrieve.context import build_context
from katsi_core.retrieve.search import search
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.synth import SynthConfigError, build_synthesizer
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.brief import BriefService
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    CapabilityGrant,
    CapabilityOperationClass,
    Claim,
    ClaimStatus,
    OpenWorkStatus,
    PortableProjectState,
    RiskClass,
    WorkspaceRecordKind,
    WorkspaceRecordStatus,
)
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.leases import WorkLeaseService
from katsi_core.workspace.observer import WatchdogObserver
from katsi_core.workspace.portable_state import PortableStateStore as PortableStateService
from katsi_core.workspace.reconcile import WorkspaceReconciler
from katsi_core.workspace.records import WorkspaceRecordService

logger = logging.getLogger(__name__)

app = typer.Typer(help="katsi: local-first relational file context.", no_args_is_help=True)
console = Console()


def _open_workspace_database(svc: dict) -> WorkspaceSQLite:
    s = svc["settings"]
    database = WorkspaceSQLite(s.store.data_dir / s.workspace.sqlite.filename, s.workspace.sqlite)
    with database.connection() as connection:
        apply_migrations(connection, s.workspace.sqlite.schema_version)
    return database


def _authenticate(svc: dict) -> object:
    credential = os.environ.get(svc["settings"].mcp.agent_credential_env)
    if not credential:
        raise KeyError("authenticated_identity")
    return svc["identity_service"].authenticate(credential)


_FACTORIES = {
    "settings": lambda _: get_settings(),
    "embed": lambda svc: EmbedClient(svc["settings"]),
    "llm": lambda svc: LLMClient(svc["settings"]),
    "graph": lambda svc: GraphStore(svc["settings"].store.data_dir / svc["settings"].store.kuzu_db),
    "vectors": lambda svc: VectorStore(
        svc["settings"].store.data_dir / "vectors", svc["settings"].store.lancedb_table
    ),
    "records": lambda svc: FileRecordStore(svc["settings"].store.data_dir / "records"),
    "workspace_database": _open_workspace_database,
    "representation_registry": lambda svc: RepresentationRegistry(svc["workspace_database"]),
    "workspace_repository": lambda svc: WorkspaceRepository(svc["workspace_database"]),
    "identity_service": lambda svc: IdentityService(svc["workspace_database"]),
    "authenticated_identity": _authenticate,
    "authorization_service": lambda svc: AuthorizationService(svc["workspace_database"]),
    "claim_service": lambda svc: ClaimService(
        svc["workspace_database"], svc["identity_service"], svc["authorization_service"]
    ),
    "lease_service": lambda svc: WorkLeaseService(
        svc["workspace_database"], svc["identity_service"], svc["settings"].lease
    ),
    "record_service": lambda svc: WorkspaceRecordService(
        svc["workspace_database"], svc["identity_service"]
    ),
    "brief_service": lambda svc: BriefService(
        svc["workspace_repository"],
        svc["workspace_database"],
        svc["record_service"],
        svc["claim_service"],
        svc["record_service"],
        svc["lease_service"],
        svc["settings"].workspace.brief,
    ),
    "portable_state_service": lambda svc: PortableStateService(
        svc["settings"].workspace.portable_state.relative_path
    ),
    "pipeline": lambda svc: IngestPipeline(
        svc["settings"],
        graph=svc["graph"],
        vectors=svc["vectors"],
        embed=svc["embed"],
        llm=svc["llm"],
        records=svc["records"],
    ),
}


class _Services(dict):
    """Service container that builds each entry on first access.

    Eager construction made every command take kuzu's single-writer lock and
    open the vector store, even `index --reprocess-media`, which touches
    neither: a media run then blocked any other katsi command on the same
    store. Tests still pre-populate entries with plain `dict.update`.
    """

    def __missing__(self, key: str):
        factory = _FACTORIES.get(key)
        if factory is None:
            raise KeyError(key)
        value = factory(self)
        self[key] = value
        return value


_state: dict = _Services()


def _services() -> dict:
    """Return the shared service container; entries are built when first used."""
    return _state


def _index_tree(svc: dict, path: Path) -> dict[str, int]:
    """Index matching files, preserving the pipeline's content-hash cache."""
    settings = svc["settings"]
    files = walk_files(path, settings.ingest.include_globs, settings.ingest.exclude_globs)
    counts = {"indexed": 0, "skipped": 0, "error": 0, "stale": 0}
    pipeline = svc["pipeline"]
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]indexing", total=len(files) or None)
        for file_path in files:
            progress.update(task, description=str(file_path.name)[:40])
            try:
                record = pipeline.index_file(file_path)
            except Exception as exc:
                logger.warning("index %s failed: %r", file_path, exc)
                counts["error"] += 1
            else:
                if record.status.value == "indexed":
                    counts["indexed"] += 1
                elif record.status.value == "error":
                    counts["error"] += 1
                elif record.status.value == "stale":
                    counts["stale"] += 1
                elif record.status.value == "skipped":
                    counts["skipped"] += 1
            progress.update(task, advance=1)
    return counts


def _print_index_summary(counts: dict[str, int]) -> None:
    table = Table(title="Index summary")
    table.add_column("status")
    table.add_column("count", justify="right")
    for status, count in counts.items():
        table.add_row(status, str(count))
    console.print(table)


def _reprocess_media(svc: dict, path: Path) -> ReprocessCounts:
    """Reprocess current tracked media under PATH without changing source state."""
    requested = path.resolve()
    reprocessor = MediaReprocessor(svc["representation_registry"], svc["settings"].media)
    total = ReprocessCounts()
    with svc["workspace_database"].connection() as connection:
        rows = connection.execute(
            """
            SELECT rv.id, rv.content_hash, r.current_path, w.root_path
            FROM resource_versions AS rv
            JOIN resources AS r ON r.id = rv.resource_id
            JOIN workspaces AS w ON w.id = r.workspace_id
            WHERE r.status = 'current'
              AND rv.observed_at = (
                  SELECT MAX(observed_at) FROM resource_versions WHERE resource_id = r.id
              )
            """
        ).fetchall()
    resources = [
        (row, file_path)
        for row in rows
        if (file_path := (Path(row["root_path"]) / row["current_path"]).resolve()).is_relative_to(
            requested
        )
        and file_path.is_file()
    ]
    console.print(f"[bold]reprocessing[/] {len(resources)} tracked file(s) under {path}")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]reprocessing", total=len(resources) or None)
        # Media pipelines are subprocess-bound (magick, tesseract, ffmpeg), so
        # threads overlap their wall time; SQLite runs in WAL with a busy
        # timeout and hands out one connection per call.
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            futures = {
                pool.submit(
                    reprocessor.process, file_path, UUID(row["id"]), row["content_hash"]
                ): file_path
                for row, file_path in resources
            }
            for future in as_completed(futures):
                progress.update(task, description=str(futures[future].name)[:40])
                outcome = future.result()
                for name in total.__dataclass_fields__:
                    setattr(total, name, getattr(total, name) + getattr(outcome, name))
                progress.update(task, advance=1)
    return total


def _ffmpeg_path(settings) -> str:
    """The ffmpeg the owner already configured for video pipelines, else PATH."""
    for definition in settings.media.pipelines:
        executable = definition.executable_path or ""
        if executable.endswith("ffmpeg"):
            return executable
    return "ffmpeg"


@dataclass
class _CaptionCounts:
    videos: int = 0
    captions: int = 0
    skipped: int = 0
    failed: int = 0


def _video_resources(svc: dict, requested: Path) -> list[tuple]:
    """Current tracked video resources under PATH: (resource_version_id, path)."""
    with svc["workspace_database"].connection() as connection:
        rows = connection.execute(
            """
            SELECT rv.id, rv.content_hash, r.current_path, w.root_path
            FROM resource_versions AS rv
            JOIN resources AS r ON r.id = rv.resource_id
            JOIN workspaces AS w ON w.id = r.workspace_id
            WHERE r.status = 'current'
              AND rv.observed_at = (
                  SELECT MAX(observed_at) FROM resource_versions WHERE resource_id = r.id
              )
            """
        ).fetchall()
    resources = []
    for row in rows:
        file_path = (Path(row["root_path"]) / row["current_path"]).resolve()
        mime, _ = mimetypes.guess_type(file_path.name)
        if (
            file_path.is_relative_to(requested)
            and file_path.is_file()
            and mime is not None
            and mime.startswith("video/")
        ):
            resources.append((row, file_path))
    return resources


def _caption_videos(svc: dict, path: Path, max_frames: int, force: bool = False) -> _CaptionCounts:
    """Caption keyframes of tracked videos under PATH and project them to search.

    Captioning is first-party (a local vision model), so it runs outside the
    network-denied media-pipeline sandbox -- unlike scene detection or OCR.

    Videos that already carry a current caption are skipped unless *force* is
    set, so an interrupted run resumes at the remaining clips instead of paying
    the whole model cost again.
    """
    s = svc["settings"]
    registry = svc["representation_registry"]
    vectors = svc["vectors"]
    embed = svc["embed"]
    captioner = VisionCaptioner(
        model=s.ollama.caption_model, host=s.ollama.host, timeout=s.ollama.timeout
    )
    ffmpeg = _ffmpeg_path(s)
    resources = _video_resources(svc, path.resolve())
    counts = _CaptionCounts()
    console.print(f"[bold]captioning[/] {len(resources)} tracked video(s) under {path}")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]captioning", total=len(resources) or None)
        for row, file_path in resources:
            progress.update(task, description=str(file_path.name)[:40])
            descriptor = registry.get_current_representation(
                UUID(row["id"]), MediaRepresentationKind.MEDIA_DESCRIPTOR
            )
            duration_ms = _duration_ms(descriptor) if descriptor is not None else None
            if not duration_ms:
                # No metadata yet: run `index --reprocess-media` first.
                counts.skipped += 1
                progress.update(task, advance=1)
                continue
            if (
                not force
                and registry.get_current_representation(
                    UUID(row["id"]), MediaRepresentationKind.IMAGE_CAPTION
                )
                is not None
            ):
                counts.skipped += 1
                progress.update(task, advance=1)
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="katsi_caption_") as tmp:
                    reps = caption_video(
                        file_path,
                        resource_version_id=UUID(row["id"]),
                        content_hash=row["content_hash"],
                        duration_ms=duration_ms,
                        ffmpeg_path=ffmpeg,
                        captioner=captioner,
                        working_dir=Path(tmp),
                        settings=s.media.media_sampling,
                        max_frames=max_frames,
                    )
                if not reps:
                    counts.failed += 1
                    progress.update(task, advance=1)
                    continue
                registry.register_representation_batch(reps)
                embeddings = embed.embed([rep.textual_payload or "" for rep in reps])
                vectors.upsert_media_text(reps, embeddings)
                counts.videos += 1
                counts.captions += len(reps)
            except Exception as exc:
                logger.warning("caption %s failed: %r", file_path, exc)
                counts.failed += 1
            progress.update(task, advance=1)
    return counts


@app.command()
def caption(
    path: Path = typer.Argument(..., help="File or directory of tracked videos."),  # noqa: B008
    max_frames: int = typer.Option(
        3, "--max-frames", help="Keyframes to caption per video (cost is ~1 model call each)."
    ),
    force: bool = typer.Option(
        False, "--force", help="Re-caption videos that already have a current caption."
    ),
) -> None:
    """Caption video keyframes with a local vision model and index them for search.

    Videos must already be tracked and have metadata (run `index` then
    `index --reprocess-media` first). Captions become semantically searchable
    through the media-text projection. Already-captioned videos are skipped
    unless --force is given, so an interrupted run resumes cheaply.
    """
    svc = _services()
    if not path.exists():
        console.print(f"[red]error:[/] path not found: {path}")
        raise typer.Exit(code=1) from None
    counts = _caption_videos(svc, path, max_frames, force=force)
    table = Table(title="Caption summary")
    table.add_column("metric")
    table.add_column("count", justify="right")
    for name, value in vars(counts).items():
        table.add_row(name, str(value))
    console.print(table)


@app.command()
def index(
    path: Path = typer.Argument(..., help="File or directory to index."),  # noqa: B008
    reprocess_media: bool = typer.Option(
        False,
        "--reprocess-media",
        help="Run configured local media pipelines for current tracked resources.",
    ),
) -> None:
    """Index PATH recursively, honoring include/exclude globs from config."""
    svc = _services()
    s = svc["settings"]
    if not path.exists():
        console.print(f"[red]error:[/] path not found: {path}")
        raise typer.Exit(code=1) from None
    if reprocess_media:
        _print_index_summary(dict(vars(_reprocess_media(svc, path))))
        return
    files = walk_files(path, s.ingest.include_globs, s.ingest.exclude_globs)
    console.print(f"[bold]indexing[/] {len(files)} file(s) under {path}")
    _print_index_summary(_index_tree(svc, path))


@app.command(name="start")
def start_cmd(
    path: Path = typer.Argument(Path("."), help="Project folder to open and ingest."),  # noqa: B008
    watch: bool = typer.Option(
        False, "--watch", help="Keep reconciling and ingesting external changes."
    ),  # noqa: B008
    display_name: str | None = typer.Option(None, "--name", help="Workspace display name."),  # noqa: B008
) -> None:
    """Open a project, reconcile its files, and ingest configured content."""
    svc = _services()
    root = path.resolve()
    if not root.is_dir():
        console.print(f"[red]error:[/] project folder not found: {path}")
        raise typer.Exit(code=1) from None
    try:
        database = svc["workspace_database"]
        with database.connection() as connection:
            existing = connection.execute(
                "SELECT id FROM workspaces WHERE root_path = ?", (str(root),)
            ).fetchone()
        if existing is None:
            workspace = svc["workspace_repository"].register_workspace(
                root, display_name or f"workspace-{root.name}"
            )
            action = "created"
        else:
            workspace = svc["workspace_repository"].get_workspace(UUID(existing["id"]))
            assert workspace is not None
            action = "opened"

        reconciler = WorkspaceReconciler(
            svc["workspace_repository"],
            svc["settings"].ingest,
            svc["settings"].workspace.observer,
            svc["claim_service"],
        )
        console.print(f"[green]workspace {action}:[/] {workspace.display_name} ({workspace.id})")
        with console.status(f"[cyan]reconciling {root}"):
            reconciler.on_startup(workspace.id)
        tracked = len(svc["workspace_repository"].list_current_resources(workspace.id))
        console.print(f"[bold]tracked[/] {tracked} file(s)")
        _print_index_summary(_index_tree(svc, root))
        console.print("[dim]Ready: use `katsi search`, `katsi ask`, or `katsi workspace-brief`.[/]")

        if not watch:
            return

        observer = WatchdogObserver()

        def on_event(event: object) -> None:
            reconciler.handle_event(workspace.id, event)
            candidate = getattr(event, "destination_path", None) or getattr(event, "path", None)
            if (
                candidate is not None
                and candidate.is_file()
                and matches_any(str(candidate), svc["settings"].ingest.include_globs)
                and not matches_any(str(candidate), svc["settings"].ingest.exclude_globs)
            ):
                svc["pipeline"].index_file(candidate)

        observer.start(root, on_event)
        console.print("[green]watching for external changes[/] — press Ctrl-C to stop")
        try:
            Event().wait()
        except KeyboardInterrupt:
            console.print("\n[dim]watch stopped[/]")
        finally:
            observer.stop()
    except Exception as exc:
        console.print(f"[red]start failed:[/] {exc}")
        raise typer.Exit(code=1) from None


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


# --- Owner commands for workspace coordination ---


def _print_json(payload: object) -> None:
    console.print_json(json.dumps(payload, default=str))


def _authenticated_identity(svc: dict) -> object:
    # A missing credential raises KeyError out of the lazy factory; both that
    # and a pre-populated None mean the same thing to the caller.
    try:
        identity = svc["authenticated_identity"]
    except KeyError:
        identity = None
    if identity is None:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")
    return identity


def _authorize_media_read(svc: dict, workspace_id: str) -> None:
    identity = _authenticated_identity(svc)
    try:
        svc["identity_service"].authorize(
            identity.id,
            UUID(workspace_id),
            CapabilityOperationClass.READ,
            None,
            RiskClass.LOW,
        )
    except Exception as exc:
        raise PermissionError("authorization denied for media read") from exc


@app.command(name="media-preview")
def media_preview_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID containing the media."),  # noqa: B008
    representation_id: str = typer.Argument(..., help="Derived representation UUID."),  # noqa: B008
    max_chars: int = typer.Option(480, "--max-chars", min=1, max=4_096),  # noqa: B008
) -> None:
    """Show a bounded cited media preview without returning media bytes."""
    svc = _services()
    try:
        _authorize_media_read(svc, workspace_id)
        registry = svc["representation_registry"]
        representation = registry.get_representation(UUID(representation_id))
        if representation is None or not registry.is_current(UUID(representation_id)):
            raise ValueError("representation is unavailable or no longer current")
        preview = None
        if representation.textual_payload is not None:
            text = " ".join(representation.textual_payload.split())
            preview = text if len(text) <= max_chars else f"{text[: max_chars - 1].rstrip()}…"
        _print_json(
            {
                "resource_version_id": str(representation.resource_version_id),
                "representation_id": str(representation.id),
                "kind": representation.kind.value,
                "status": representation.status.value,
                "locators": [
                    locator.model_dump(mode="json") for locator in representation.locators
                ],
                "coverage_fraction": representation.coverage.coverage_fraction,
                "preview": preview,
                "thumbnail_reference": (
                    representation.blob_reference
                    if representation.kind.value == "thumbnail"
                    else None
                ),
            }
        )
    except Exception as exc:
        console.print(f"[red]media preview failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="media-original")
def media_original_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID containing the media."),  # noqa: B008
    resource_version_id: str = typer.Argument(..., help="Resource-version UUID to resolve."),  # noqa: B008
) -> None:
    """Resolve a cited media original without returning its bytes."""
    svc = _services()
    try:
        _authorize_media_read(svc, workspace_id)
        with svc["workspace_database"].connection() as connection:
            row = connection.execute(
                """
                SELECT resources.id AS resource_id, resources.current_path, resource_versions.content_hash
                FROM resource_versions JOIN resources ON resources.id = resource_versions.resource_id
                WHERE resource_versions.id = ? AND resources.workspace_id = ?
                """,
                (str(UUID(resource_version_id)), workspace_id),
            ).fetchone()
        if row is None:
            raise ValueError("unknown resource version in workspace")
        _print_json(
            {
                "resource_id": row["resource_id"],
                "resource_version_id": resource_version_id,
                "path": row["current_path"],
                "content_hash": row["content_hash"],
            }
        )
    except Exception as exc:
        console.print(f"[red]media original lookup failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="inspect-workspace")
def inspect_workspace_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to inspect."),  # noqa: B008
) -> None:
    """Display workspace metadata and its ten most recent events."""
    svc = _services()
    try:
        workspace = svc["workspace_repository"].get_workspace(UUID(workspace_id))
        if workspace is None:
            raise ValueError(f"workspace not found: {workspace_id}")
        events = svc["workspace_repository"].recent_events(UUID(workspace_id), limit=10)
        _print_json(
            {
                **workspace.model_dump(mode="json"),
                "recent_events": [event.model_dump(mode="json") for event in events],
            }
        )
    except Exception as exc:
        console.print(f"[red]workspace inspection failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="workspace-brief")
def workspace_brief_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to summarize."),  # noqa: B008
    byte_budget: int = typer.Option(100_000, "--byte-budget", help="Maximum brief size in bytes."),  # noqa: B008
) -> None:
    """Return the budget-bounded authoritative workspace brief."""
    svc = _services()
    try:
        brief = svc["brief_service"].assemble(UUID(workspace_id), byte_budget=byte_budget)
        _print_json(brief.model_dump(mode="json"))
    except Exception as exc:
        console.print(f"[red]workspace brief failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="publish-claim")
def publish_claim_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
    text: str = typer.Argument(..., help="Claim text."),  # noqa: B008
    scope: str = typer.Option("", "--scope", help="Comma-separated workspace-relative paths."),  # noqa: B008
    confidence: float = typer.Option(0.8, "--confidence", min=0, max=1),  # noqa: B008
) -> None:
    """Publish a capability-checked proposed claim."""
    from datetime import UTC, datetime
    from uuid import uuid4

    svc = _services()
    try:
        identity = _authenticated_identity(svc)
        workspace = UUID(workspace_id)
        svc["identity_service"].authorize(
            identity.id, workspace, CapabilityOperationClass.CLAIM, None, RiskClass.LOW
        )
        claim = Claim(
            id=uuid4(),
            workspace_id=workspace,
            author_id=identity.id,
            text=text,
            scope_paths=tuple(path.strip() for path in scope.split(",") if path.strip()),
            confidence=confidence,
            status=ClaimStatus.PROPOSED,
            created_at=datetime.now(UTC),
        )
        _print_json(svc["claim_service"].publish(claim).model_dump(mode="json"))
    except Exception as exc:
        console.print(f"[red]claim publication failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="list-claims")
def list_claims_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
    status: str | None = typer.Option(None, "--status", help="Optional claim status filter."),  # noqa: B008
) -> None:
    """List claims for a workspace."""
    svc = _services()
    try:
        claims = svc["claim_service"].list_for_workspace(UUID(workspace_id))
        if status is not None:
            claims = [claim for claim in claims if claim.status.value == status]
        _print_json([claim.model_dump(mode="json") for claim in claims])
    except Exception as exc:
        console.print(f"[red]claim listing failed:[/] {exc}")
        raise typer.Exit(code=1) from None


def _list_workspace_records(
    workspace_id: str, kind: WorkspaceRecordKind, status: str | None
) -> list[dict]:
    svc = _services()
    records = svc["record_service"].list_records(UUID(workspace_id))
    return [
        record.model_dump(mode="json")
        for record in records
        if record.kind == kind and (status is None or record.status.value == status)
    ]


@app.command(name="inspect-decisions")
def inspect_decisions_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
    status: str | None = typer.Option(None, "--status", help="Optional decision status filter."),  # noqa: B008
) -> None:
    """List decisions recorded for a workspace."""
    try:
        _print_json(_list_workspace_records(workspace_id, WorkspaceRecordKind.DECISION, status))
    except Exception as exc:
        console.print(f"[red]decision inspection failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="inspect-blockers")
def inspect_blockers_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
) -> None:
    """List open blockers for a workspace."""
    try:
        _print_json(
            _list_workspace_records(
                workspace_id, WorkspaceRecordKind.BLOCKER, WorkspaceRecordStatus.OPEN.value
            )
        )
    except Exception as exc:
        console.print(f"[red]blocker inspection failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="inspect-open-work")
def inspect_open_work_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
) -> None:
    """List open and blocked work items for a workspace."""
    svc = _services()
    try:
        work = svc["record_service"].list_open_work(UUID(workspace_id))
        _print_json(
            [
                item.model_dump(mode="json")
                for item in work
                if item.status in {OpenWorkStatus.OPEN, OpenWorkStatus.BLOCKED}
            ]
        )
    except Exception as exc:
        console.print(f"[red]open work inspection failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="acquire-lease")
def acquire_lease_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
    task: str = typer.Argument(..., help="Work being leased."),  # noqa: B008
    scope: str = typer.Option("", "--scope", help="Comma-separated workspace-relative paths."),  # noqa: B008
) -> None:
    """Acquire an advisory work lease for the authenticated identity."""
    svc = _services()
    try:
        identity = _authenticated_identity(svc)
        workspace = UUID(workspace_id)
        svc["identity_service"].authorize(
            identity.id, workspace, CapabilityOperationClass.LEASE, None, RiskClass.LOW
        )
        lease = svc["lease_service"].acquire(
            workspace,
            identity.id,
            task,
            tuple(path.strip() for path in scope.split(",") if path.strip()),
        )
        _print_json(lease.model_dump(mode="json"))
    except Exception as exc:
        console.print(f"[red]lease acquisition failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="renew-lease")
def renew_lease_cmd(
    lease_id: str = typer.Argument(..., help="Lease UUID."),  # noqa: B008
    expected_expires_at: str = typer.Argument(..., help="Current ISO-8601 expiration."),  # noqa: B008
) -> None:
    """Renew an active work lease using its current expiration value."""
    from datetime import datetime

    svc = _services()
    try:
        identity = _authenticated_identity(svc)
        lease = svc["lease_service"].renew(
            UUID(lease_id), identity.id, datetime.fromisoformat(expected_expires_at)
        )
        _print_json(lease.model_dump(mode="json"))
    except Exception as exc:
        console.print(f"[red]lease renewal failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="release-lease")
def release_lease_cmd(
    lease_id: str = typer.Argument(..., help="Lease UUID."),  # noqa: B008
) -> None:
    """Release an active work lease held by the authenticated identity."""
    svc = _services()
    try:
        identity = _authenticated_identity(svc)
        _print_json(
            svc["lease_service"].release(UUID(lease_id), identity.id).model_dump(mode="json")
        )
    except Exception as exc:
        console.print(f"[red]lease release failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="inspect-active-leases")
def inspect_active_leases_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID."),  # noqa: B008
) -> None:
    """List active advisory work leases for a workspace."""
    svc = _services()
    try:
        _print_json(
            [
                lease.model_dump(mode="json")
                for lease in svc["lease_service"].active_for_workspace(UUID(workspace_id))
            ]
        )
    except Exception as exc:
        console.print(f"[red]lease inspection failed:[/] {exc}")
        raise typer.Exit(code=1) from None


@app.command(name="workspace")
def workspace_cmd(
    root: PathLib = typer.Argument(..., help="Root path of the workspace."),  # noqa: B008
    display_name: str = typer.Option(None, "--name", help="Display name for the workspace."),  # noqa: B008
) -> None:
    """Register a workspace and display its identity information.

    Creates a stable workspace identity for the given root path.
    If the workspace already exists, displays its current state.
    """
    svc = _services()
    root_resolved = root.resolve()

    # Try to find existing workspace
    database = svc["workspace_database"]
    with database.connection() as connection:
        existing = connection.execute(
            "SELECT * FROM workspaces WHERE root_path = ?", (str(root_resolved),)
        ).fetchone()

    if existing:
        table = Table(title="existing workspace")
        table.add_column("property")
        table.add_column("value")
        table.add_row("workspace_id", existing["id"])
        table.add_row("display_name", existing["display_name"])
        table.add_row("status", existing["status"])
        table.add_row("state_version", str(existing["state_version"]))
        table.add_row("root_path", existing["root_path"])
        table.add_row("created_at", existing["created_at"])
        table.add_row("updated_at", existing["updated_at"])
        console.print(table)
        return

    # Register new workspace
    name = display_name or f"workspace-{root_resolved.name}"
    try:
        workspace = svc["workspace_repository"].register_workspace(root_resolved, name)
        table = Table(title="workspace registered")
        table.add_column("property")
        table.add_column("value")
        table.add_row("workspace_id", str(workspace.id))
        table.add_row("display_name", workspace.display_name)
        table.add_row("status", workspace.status)
        table.add_row("state_version", str(workspace.state_version))
        table.add_row("root_path", workspace.root_path)
        table.add_row("created_at", workspace.created_at.isoformat())
        console.print(table)
    except Exception as e:
        console.print(f"[red]workspace registration failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="export-state")
def export_state_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to export state for."),  # noqa: B008
    output: PathLib = typer.Option(  # noqa: B008
        None, "--output", "-o", help="Output file path (default: stdout)."
    ),
) -> None:
    """Export portable project state from a workspace.

    Creates a portable state bundle containing workspace intent and metadata
    that can travel with the workspace or be imported elsewhere.
    """
    import json

    svc = _services()

    try:
        state = svc["portable_state_service"].export_state(UUID(workspace_id))
        state_dict = {
            "schema_version": state.schema_version,
            "workspace_id": str(state.workspace_id),
            "display_name": state.display_name,
            "active_intent": state.active_intent,
            "invariant_definitions": state.invariant_definitions,
            "verified_decisions": state.verified_decisions,
            "selected_metadata": state.selected_metadata,
        }

        if output:
            output.write_text(json.dumps(state_dict, indent=2))
            console.print(f"[green]state exported to:[/] {output}")
        else:
            console.print(json.dumps(state_dict, indent=2))
    except Exception as e:
        console.print(f"[red]state export failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="import-state")
def import_state_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to import state into."),  # noqa: B008
    input_file: PathLib = typer.Argument(..., help="Input JSON file with portable state."),  # noqa: B008
) -> None:
    """Import portable project state into a workspace.

    Loads a previously exported portable state bundle and applies it
    to the target workspace.
    """
    import json

    svc = _services()

    try:
        state_dict = json.loads(input_file.read_text())
        state = PortableProjectState(
            schema_version=state_dict["schema_version"],
            workspace_id=UUID(state_dict["workspace_id"]),
            display_name=state_dict["display_name"],
            active_intent=state_dict.get("active_intent"),
            invariant_definitions=tuple(state_dict.get("invariant_definitions", [])),
            verified_decisions=tuple(state_dict.get("verified_decisions", [])),
            selected_metadata=state_dict.get("selected_metadata", {}),
        )

        svc["portable_state_service"].import_state(UUID(workspace_id), state)
        console.print(f"[green]state imported into workspace:[/] {workspace_id}")
    except Exception as e:
        console.print(f"[red]state import failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="issue-identity")
def issue_identity_cmd(
    display_name: str = typer.Option(..., "--name", help="Display name for the identity."),  # noqa: B008
    client_name: str = typer.Option(..., "--client", help="Client application name."),  # noqa: B008
    model_name: str = typer.Option(None, "--model", help="Optional model name."),  # noqa: B008
) -> None:
    """Issue a new agent identity and credential.

    Creates a new agent identity with a secret credential.
    The credential is displayed only once and must be stored securely.
    """
    svc = _services()

    try:
        identity = svc["identity_service"].register(
            display_name=display_name,
            client_name=client_name,
            model_name=model_name,
        )

        issued = svc["identity_service"].issue_credential(identity.id)

        table = Table(title="agent identity issued")
        table.add_column("property")
        table.add_column("value")
        table.add_row("identity_id", str(issued.identity.id))
        table.add_row("display_name", issued.identity.display_name)
        table.add_row("client_name", issued.identity.client_name)
        table.add_row("model_name", str(issued.identity.model_name or ""))
        table.add_row("active", "yes" if issued.identity.active else "no")
        table.add_row("created_at", issued.identity.created_at.isoformat())
        console.print(table)

        console.print("\n[bold yellow]credential (display once):[/]")
        console.print(f"[white on black]{issued.credential}[/]")

        console.print("\n[bold]configure with:[/]")
        console.print(f'  export KATSI_AGENT_CREDENTIAL="{issued.credential}"')
    except Exception as e:
        console.print(f"[red]identity issuance failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="rotate-credential")
def rotate_credential_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to rotate credential for."),  # noqa: B008
) -> None:
    """Rotate an existing identity's credential.

    Invalidates all existing credentials and issues a new one.
    The new credential is displayed only once.
    """

    svc = _services()

    try:
        issued = svc["identity_service"].rotate_credential(UUID(identity_id))

        table = Table(title="credential rotated")
        table.add_column("property")
        table.add_column("value")
        table.add_row("identity_id", str(issued.identity.id))
        table.add_row("display_name", issued.identity.display_name)
        table.add_row("active", "yes" if issued.identity.active else "no")
        console.print(table)

        console.print("\n[bold yellow]new credential (display once):[/]")
        console.print(f"[white on black]{issued.credential}[/]")

        console.print("\n[bold]configure with:[/]")
        console.print(f'  export KATSI_AGENT_CREDENTIAL="{issued.credential}"')
    except Exception as e:
        console.print(f"[red]credential rotation failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="revoke-identity")
def revoke_identity_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to revoke."),  # noqa: B008
) -> None:
    """Revoke an agent identity and all its credentials.

    Immediately deactivates the identity and invalidates all credentials.
    This action cannot be undone.
    """

    svc = _services()

    try:
        svc["identity_service"].revoke(UUID(identity_id))
        console.print(f"[green]identity revoked:[/] {identity_id}")
    except Exception as e:
        console.print(f"[red]identity revocation failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="list-identities")
def list_identities_cmd() -> None:
    """List all agent identities with their status.

    Shows all registered identities with active/revoked status.
    Credentials are never displayed.
    """
    svc = _services()
    database = svc["workspace_database"]

    try:
        with database.connection() as conn:
            rows = conn.execute("SELECT * FROM agent_identities ORDER BY created_at").fetchall()

        if not rows:
            console.print("[yellow]no identities found[/]")
            return

        table = Table(title="agent identities")
        table.add_column("identity_id")
        table.add_column("display_name")
        table.add_column("client_name")
        table.add_column("model_name")
        table.add_column("active")
        table.add_column("created_at")

        for row in rows:
            table.add_row(
                row["id"],
                row["display_name"],
                row["client_name"],
                str(row["model_name"] or ""),
                "[green]yes[/]" if row["active"] else "[red]no[/]",
                row["created_at"],
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]list identities failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="grant-capability")
def grant_capability_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to grant capability to."),  # noqa: B008
    workspace_id: str = typer.Argument(..., help="Workspace UUID for the capability."),  # noqa: B008
    operations: str = typer.Argument(
        ...,
        help="Comma-separated operation classes (read,claim,lease,change_set,governed_execution).",
    ),  # noqa: B008
    resource_scope: str = typer.Option(
        "", "--scope", help="Comma-separated path scope (default: all paths)."
    ),  # noqa: B008
    max_risk: str = typer.Option("low", "--risk", help="Maximum risk level (low, medium, high)."),  # noqa: B008
) -> None:
    """Grant a capability to an identity for a workspace.

    Creates a capability grant allowing specific operations within
    optional resource scope and risk limits.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    svc = _services()

    try:
        # Parse operation classes
        op_classes = frozenset(
            CapabilityOperationClass(op.strip()) for op in operations.split(",") if op.strip()
        )

        # Parse resource scope
        scope_paths = (
            tuple(p.strip() for p in resource_scope.split(",") if p.strip())
            if resource_scope
            else ()
        )

        # Parse risk class
        risk = RiskClass(max_risk.lower())

        grant = CapabilityGrant(
            id=uuid4(),
            identity_id=UUID(identity_id),
            workspace_id=UUID(workspace_id),
            operation_classes=op_classes,
            resource_scope=scope_paths,
            maximum_risk=risk,
            issued_at=datetime.now(UTC),
        )

        svc["identity_service"].grant(grant)

        table = Table(title="capability granted")
        table.add_column("property")
        table.add_column("value")
        table.add_row("grant_id", str(grant.id))
        table.add_row("identity_id", str(grant.identity_id))
        table.add_row("workspace_id", str(grant.workspace_id))
        table.add_row("operations", ", ".join(op.value for op in op_classes))
        table.add_row("resource_scope", ", ".join(scope_paths) if scope_paths else "(all paths)")
        table.add_row("maximum_risk", risk.value)
        table.add_row("issued_at", grant.issued_at.isoformat())
        console.print(table)
    except Exception as e:
        console.print(f"[red]capability grant failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="revoke-grant")
def revoke_grant_cmd(
    grant_id: str = typer.Argument(..., help="Grant UUID to revoke."),  # noqa: B008
) -> None:
    """Revoke a capability grant.

    Immediately removes the grant. The grant record is preserved for audit.
    """

    svc = _services()

    try:
        svc["identity_service"].revoke_grant(UUID(grant_id))
        console.print(f"[green]capability grant revoked:[/] {grant_id}")
    except Exception as e:
        console.print(f"[red]grant revocation failed:[/] {e}")
        raise typer.Exit(code=1) from None


@app.command(name="inspect-capabilities")
def inspect_capabilities_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to inspect capabilities for."),  # noqa: B008
    workspace_id: str = typer.Option(None, "--workspace", help="Filter by workspace UUID."),  # noqa: B008
) -> None:
    """Inspect capabilities granted to an identity.

    Shows all active capability grants for the identity, optionally
    filtered to a specific workspace.
    """

    svc = _services()
    database = svc["workspace_database"]

    try:
        query = "SELECT * FROM capability_grants WHERE identity_id = ? AND revoked_at IS NULL"
        params = [str(identity_id)]

        if workspace_id:
            query += " AND workspace_id = ?"
            params.append(str(workspace_id))

        query += " ORDER BY issued_at"
        with database.connection() as conn:
            rows = conn.execute(query, params).fetchall()

        if not rows:
            console.print("[yellow]no active capabilities found[/]")
            return

        table = Table(title="active capabilities")
        table.add_column("grant_id")
        table.add_column("workspace_id")
        table.add_column("operations")
        table.add_column("resource_scope")
        table.add_column("maximum_risk")
        table.add_column("issued_at")
        table.add_column("expires_at")

        for row in rows:
            operations = ", ".join(json.loads(row["operation_classes_json"]))
            scope = ", ".join(json.loads(row["resource_scope_json"])) or "(all paths)"

            table.add_row(
                row["id"][:8],
                row["workspace_id"][:8],
                operations,
                scope,
                row["maximum_risk"],
                row["issued_at"],
                row["expires_at"] or "(never)",
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]capability inspection failed:[/] {e}")
        raise typer.Exit(code=1) from None


def main() -> None:
    """Entry point: `katsi` console script."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app()
