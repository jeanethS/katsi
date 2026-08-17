"""katsi CLI: index, status, search, ask."""

from __future__ import annotations

import fnmatch
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import LLMClient
from katsi_core.config import get_settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.retrieve.context import build_context
from katsi_core.retrieve.search import search
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.synth import SynthConfigError, build_synthesizer
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.contracts import (
    AgentIdentity,
    CapabilityGrant,
    CapabilityOperationClass,
    PortableProjectState,
    RiskClass,
)
from katsi_core.workspace.portable_state import PortableStateStore as PortableStateService
from pathlib import Path as PathLib
from uuid import UUID

logger = logging.getLogger(__name__)

app = typer.Typer(help="katsi: local-first relational file context.", no_args_is_help=True)
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
    database = WorkspaceSQLite(s.store.data_dir / s.workspace.sqlite.filename, s.workspace.sqlite)
    with database.connection() as connection:
        apply_migrations(connection, s.workspace.sqlite.schema_version)
    _state["workspace_database"] = database
    _state["workspace_repository"] = WorkspaceRepository(database)
    _state["identity_service"] = IdentityService(database)
    _state["portable_state_service"] = PortableStateService(
        s.workspace.portable_state.relative_path
    )
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
        raise typer.Exit(code=1)


@app.command(name="export-state")
def export_state_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to export state for."),  # noqa: B008
    output: PathLib = typer.Option(None, "--output", "-o", help="Output file path (default: stdout)."),  # noqa: B008
) -> None:
    """Export portable project state from a workspace.

    Creates a portable state bundle containing workspace intent and metadata
    that can travel with the workspace or be imported elsewhere.
    """
    from uuid import UUID
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
        raise typer.Exit(code=1)


@app.command(name="import-state")
def import_state_cmd(
    workspace_id: str = typer.Argument(..., help="Workspace UUID to import state into."),  # noqa: B008
    input_file: PathLib = typer.Argument(..., help="Input JSON file with portable state."),  # noqa: B008
) -> None:
    """Import portable project state into a workspace.

    Loads a previously exported portable state bundle and applies it
    to the target workspace.
    """
    from uuid import UUID
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
        raise typer.Exit(code=1)


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
        console.print(f"  export KATSI_AGENT_CREDENTIAL=\"{issued.credential}\"")
    except Exception as e:
        console.print(f"[red]identity issuance failed:[/] {e}")
        raise typer.Exit(code=1)


@app.command(name="rotate-credential")
def rotate_credential_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to rotate credential for."),  # noqa: B008
) -> None:
    """Rotate an existing identity's credential.

    Invalidates all existing credentials and issues a new one.
    The new credential is displayed only once.
    """
    from uuid import UUID

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
        console.print(f"  export KATSI_AGENT_CREDENTIAL=\"{issued.credential}\"")
    except Exception as e:
        console.print(f"[red]credential rotation failed:[/] {e}")
        raise typer.Exit(code=1)


@app.command(name="revoke-identity")
def revoke_identity_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to revoke."),  # noqa: B008
) -> None:
    """Revoke an agent identity and all its credentials.

    Immediately deactivates the identity and invalidates all credentials.
    This action cannot be undone.
    """
    from uuid import UUID

    svc = _services()

    try:
        svc["identity_service"].revoke(UUID(identity_id))
        console.print(f"[green]identity revoked:[/] {identity_id}")
    except Exception as e:
        console.print(f"[red]identity revocation failed:[/] {e}")
        raise typer.Exit(code=1)


@app.command(name="list-identities")
def list_identities_cmd() -> None:
    """List all agent identities with their status.

    Shows all registered identities with active/revoked status.
    Credentials are never displayed.
    """
    svc = _services()
    database = svc["workspace_database"]

    try:
        rows = database.connection().execute(
            "SELECT * FROM agent_identities ORDER BY created_at"
        ).fetchall()

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
        raise typer.Exit(code=1)


@app.command(name="grant-capability")
def grant_capability_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to grant capability to."),  # noqa: B008
    workspace_id: str = typer.Argument(..., help="Workspace UUID for the capability."),  # noqa: B008
    operations: str = typer.Argument(..., help="Comma-separated operation classes (read,claim,lease,change_set,governed_execution)."),  # noqa: B008
    resource_scope: str = typer.Option("", "--scope", help="Comma-separated path scope (default: all paths)."),  # noqa: B008
    max_risk: str = typer.Option("low", "--risk", help="Maximum risk level (low, medium, high)."),  # noqa: B008
) -> None:
    """Grant a capability to an identity for a workspace.

    Creates a capability grant allowing specific operations within
    optional resource scope and risk limits.
    """
    from uuid import UUID, uuid4
    from datetime import UTC, datetime

    svc = _services()

    try:
        # Parse operation classes
        op_classes = frozenset(
            CapabilityOperationClass(op.strip())
            for op in operations.split(",")
            if op.strip()
        )

        # Parse resource scope
        scope_paths = tuple(
            p.strip() for p in resource_scope.split(",") if p.strip()
        ) if resource_scope else ()

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
        raise typer.Exit(code=1)


@app.command(name="revoke-grant")
def revoke_grant_cmd(
    grant_id: str = typer.Argument(..., help="Grant UUID to revoke."),  # noqa: B008
) -> None:
    """Revoke a capability grant.

    Immediately removes the grant. The grant record is preserved for audit.
    """
    from uuid import UUID

    svc = _services()

    try:
        svc["identity_service"].revoke_grant(UUID(grant_id))
        console.print(f"[green]capability grant revoked:[/] {grant_id}")
    except Exception as e:
        console.print(f"[red]grant revocation failed:[/] {e}")
        raise typer.Exit(code=1)


@app.command(name="inspect-capabilities")
def inspect_capabilities_cmd(
    identity_id: str = typer.Argument(..., help="Identity UUID to inspect capabilities for."),  # noqa: B008
    workspace_id: str = typer.Option(None, "--workspace", help="Filter by workspace UUID."),  # noqa: B008
) -> None:
    """Inspect capabilities granted to an identity.

    Shows all active capability grants for the identity, optionally
    filtered to a specific workspace.
    """
    from uuid import UUID

    svc = _services()
    database = svc["workspace_database"]

    try:
        query = "SELECT * FROM capability_grants WHERE identity_id = ? AND revoked_at IS NULL"
        params = [str(identity_id)]

        if workspace_id:
            query += " AND workspace_id = ?"
            params.append(str(workspace_id))

        query += " ORDER BY issued_at"
        rows = database.connection().execute(query, params).fetchall()

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
        raise typer.Exit(code=1)


def main() -> None:
    """Entry point: `katsi` console script."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app()
