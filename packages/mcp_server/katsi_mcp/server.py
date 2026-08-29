from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import LLMClient
from katsi_core.config import get_settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.media.contracts import MediaRepresentationKind
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.models import ContextBundle, FileHit, FileRecord
from katsi_core.retrieve.context import build_context
from katsi_core.retrieve.search import search
from katsi_core.store.graph import GraphStore
from katsi_core.store.projection_worker import ProjectionWorker
from katsi_core.store.vectors import VectorStore
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.synth import build_synthesizer
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.brief import BriefService
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    CapabilityOperationClass,
    Claim,
    ClaimStatus,
    RiskClass,
)
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.leases import WorkLeaseService
from katsi_core.workspace.records import WorkspaceRecordService

logger = logging.getLogger(__name__)

mcp = FastMCP("katsi")


# --- shared service singletons (lazy-init on first tool call) ---

_state: dict = {}


def _services():
    """Lazily construct + share pipeline, embed, llm, graph, vectors, records."""
    if _state:
        return _state
    s = get_settings()
    _state["settings"] = s
    _state["embed"] = EmbedClient(s)
    _state["llm"] = LLMClient(s)
    _state["graph"] = GraphStore(s.store.data_dir / s.store.kuzu_db, read_only=True)
    _state["vectors"] = VectorStore(s.store.data_dir / "vectors", s.store.lancedb_table)
    _state["records"] = FileRecordStore(s.store.data_dir / "records")
    database = WorkspaceSQLite(s.store.data_dir / s.workspace.sqlite.filename, s.workspace.sqlite)
    with database.connection() as connection:
        apply_migrations(connection, s.workspace.sqlite.schema_version)
    _state["workspace_database"] = database
    _state["representation_registry"] = RepresentationRegistry(database)
    _state["workspace_repository"] = WorkspaceRepository(database)
    _state["identity_service"] = IdentityService(database)
    credential = os.environ.get(s.mcp.agent_credential_env)
    if credential:
        _state["authenticated_identity"] = _state["identity_service"].authenticate(credential)
    # Initialize workspace coordination services
    _state["authorization_service"] = AuthorizationService(database)
    _state["claim_service"] = ClaimService(
        database, _state["identity_service"], _state["authorization_service"]
    )
    _state["lease_service"] = WorkLeaseService(database, _state["identity_service"], s.lease)
    _state["record_service"] = WorkspaceRecordService(database, _state["identity_service"])
    _state["brief_service"] = BriefService(
        _state["workspace_repository"],
        database,
        _state["record_service"],  # IntentService placeholder
        _state["claim_service"],
        _state["record_service"],
        _state["lease_service"],
        s.workspace.brief,
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


def _projection_diagnostics(svc: dict) -> list[dict[str, object]]:
    """Serialize authoritative projection offsets for status and retrieval output."""
    database = svc.get("workspace_database")
    if database is None:
        return []
    worker = ProjectionWorker(database, svc["settings"].workspace.projection_worker)
    return [
        {
            "workspace_id": str(workspace_id),
            "projection_name": entry.projection_name,
            "applied_outbox_id": entry.applied_outbox_id,
            "latest_outbox_id": entry.latest_outbox_id,
            "lag": entry.lag,
            "lagging": entry.lagging,
        }
        for workspace_id, entries in worker.all_freshness().items()
        for entry in entries
    ]


# --- MCP tools ---


@mcp.tool()
def index_status() -> dict:
    """Counts by status, last index time, total chunks."""
    svc = _services()
    counts = svc["records"].count_by_status()
    text_files = sum(counts.values())
    database = svc.get("workspace_database")
    media_counts = {}
    if database is not None:
        # The registry owns the representations table, so ask it rather than
        # querying the schema behind its back. Constructing one also creates
        # the table when a caller injected a database that predates it.
        registry = svc.get("representation_registry") or RepresentationRegistry(database)
        media_counts = registry.count_current_resources_by_status(
            MediaRepresentationKind.MEDIA_DESCRIPTOR
        )
    media_files = sum(media_counts.values())
    last_indexed = None
    for rec in svc["records"].list_all():
        if rec.last_indexed_at is not None and (
            last_indexed is None or rec.last_indexed_at > last_indexed
        ):
            last_indexed = rec.last_indexed_at
    try:
        total_chunks = svc["vectors"].count()
    except Exception as e:
        logger.warning("index_status: vector count failed: %r", e)
        total_chunks = 0
    diagnostics = _projection_diagnostics(svc)
    return {
        "counts_by_status": counts,
        "media_counts_by_status": media_counts,
        "text_files": text_files,
        "media_files": media_files,
        "total_files": text_files + media_files,
        "total_chunks": total_chunks,
        "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
        "projection_diagnostics": diagnostics,
        "projection_lag": any(item["lagging"] for item in diagnostics),
    }


@mcp.tool()
def search_files(query: str, k: int = 8) -> list[FileHit]:
    """Ranked files for a query, each with a one-line 'why relevant'."""
    # NOTE: tool name is search_files to avoid shadowing the imported search().
    svc = _services()
    return search(
        query,
        k=k,
        settings=svc["settings"],
        vectors=svc["vectors"],
        graph=svc["graph"],
        embed=svc["embed"],
        records=svc["records"],
    )


def _resource_path(svc: dict, resource_version_id: str) -> str | None:
    """Resolve a media resource version back to its current on-disk path."""
    with svc["workspace_database"].connection() as conn:
        row = conn.execute(
            """
            SELECT r.current_path, w.root_path
            FROM resource_versions AS rv
            JOIN resources AS r ON r.id = rv.resource_id
            JOIN workspaces AS w ON w.id = r.workspace_id
            WHERE rv.id = ?
            """,
            (resource_version_id,),
        ).fetchone()
    if row is None:
        return None
    return str(Path(row["root_path"]) / row["current_path"])


@mcp.tool()
def search_media(query: str, k: int = 8) -> list[dict]:
    """Semantic search over media-derived text (video/image captions, OCR, transcripts).

    Complements search_files (which searches document text) by ranking the media
    resources whose captions/OCR/transcripts match the query. Each hit cites the
    source path, the matched text, and time/frame locators for building an edit.
    """
    from katsi_core.retrieve.media import (
        fuse_media_results,
        media_search_hits,
    )
    from katsi_core.retrieve.media import search_media as route

    svc = _services()
    registry = svc["representation_registry"]
    query_vector = svc["embed"].embed([query])[0]
    routed = route(svc["vectors"], text_vector=query_vector, k=k)
    # media_search_hits needs the authoritative representations to cite; gather
    # the ones the routed signals point at.
    representations = {}
    for hits in routed.values():
        for hit in hits:
            representation = registry.get_representation(hit.representation_id)
            if representation is not None:
                representations[representation.id] = representation
    return [
        {
            "path": _resource_path(svc, str(hit.resource_version_id)),
            "resource_version_id": str(hit.resource_version_id),
            "representation_id": str(hit.representation_id),
            "kind": hit.representation_kind,
            "preview": hit.preview,
            "locators": list(hit.locators),
            "score": hit.score,
        }
        for hit in media_search_hits(fuse_media_results(routed), representations, k=k)
    ]


@mcp.tool()
def get_context(query: str, max_tokens: int = 3000) -> ContextBundle:
    """PRIMARY TOOL. Curated, budget-capped context for the client to answer over:
    file summaries + the few most relevant raw chunks + a graph relationship sketch."""
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
    diagnostics = _projection_diagnostics(svc)
    return bundle.model_copy(
        update={
            "projection_diagnostics": diagnostics,
            "projection_lag": any(item["lagging"] for item in diagnostics),
        }
    )


def _authorize_media_read(svc: dict, workspace_id: str) -> None:
    """Require an authenticated READ grant before exposing cited media evidence."""
    from uuid import UUID

    identity = svc.get("authenticated_identity")
    if identity is None:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")
    try:
        svc["identity_service"].authorize(
            identity.id, UUID(workspace_id), CapabilityOperationClass.READ, None, RiskClass.LOW
        )
    except Exception as exc:
        raise PermissionError("authorization denied for media read") from exc


@mcp.tool()
def get_media_preview(workspace_id: str, representation_id: str, max_chars: int = 480) -> dict:
    """Return a capability-checked, bounded cited preview without media bytes."""
    from uuid import UUID

    if not 1 <= max_chars <= 4_096:
        raise ValueError("max_chars must be between 1 and 4096")
    svc = _services()
    _authorize_media_read(svc, workspace_id)
    registry = svc["representation_registry"]
    representation = registry.get_representation(UUID(representation_id))
    if representation is None or not registry.is_current(UUID(representation_id)):
        raise ValueError("representation is unavailable or no longer current")
    preview = None
    if representation.textual_payload is not None:
        text = " ".join(representation.textual_payload.split())
        preview = text if len(text) <= max_chars else f"{text[: max_chars - 1].rstrip()}…"
    return {
        "resource_version_id": str(representation.resource_version_id),
        "representation_id": str(representation.id),
        "kind": representation.kind.value,
        "status": representation.status.value,
        "locators": [item.model_dump(mode="json") for item in representation.locators],
        "coverage_fraction": representation.coverage.coverage_fraction,
        "preview": preview,
        "thumbnail_reference": representation.blob_reference
        if representation.kind.value == "thumbnail"
        else None,
    }


@mcp.tool()
def open_media_original(workspace_id: str, resource_version_id: str) -> dict:
    """Capability-check and resolve a cited original; never return its bytes."""
    from uuid import UUID

    svc = _services()
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
    return {
        "resource_id": row["resource_id"],
        "resource_version_id": resource_version_id,
        "path": row["current_path"],
        "content_hash": row["content_hash"],
    }


@mcp.tool()
def list_media_representations(
    workspace_id: str,
    path: str,
    kinds: list[str] | None = None,
    limit: int = 100,
) -> list[dict]:
    """Enumerate a cited path's currently-visible representations; never bytes."""
    from uuid import UUID

    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if kinds is not None:
        known = {kind.value for kind in MediaRepresentationKind}
        for kind in kinds:
            if kind not in known:
                raise ValueError(f"unknown representation kind: {kind}")

    svc = _services()
    _authorize_media_read(svc, workspace_id)
    with svc["workspace_database"].connection() as connection:
        row = connection.execute(
            """
            SELECT resource_versions.id AS resource_version_id
            FROM resource_versions JOIN resources ON resources.id = resource_versions.resource_id
            WHERE resources.workspace_id = ? AND resources.current_path = ?
            ORDER BY resource_versions.observed_at DESC
            LIMIT 1
            """,
            (workspace_id, path),
        ).fetchone()
    if row is None:
        raise ValueError("unknown path in workspace")

    registry = svc["representation_registry"]
    # is_current, not status alone: a superseded generation must never be
    # offered as usable, or get_media_preview would reject what this returned.
    representations = [
        item
        for item in registry.get_representations_by_resource(UUID(row["resource_version_id"]))
        if registry.is_current(item.id)
    ]
    if kinds is not None:
        representations = [item for item in representations if item.kind.value in kinds]

    def _earliest_start_ms(representation) -> int:
        starts = [
            locator.start_ms
            for locator in representation.locators
            if getattr(locator, "start_ms", None) is not None
        ]
        return min(starts) if starts else -1

    # Deterministic: a reindex must not reshuffle downstream shot selection.
    representations.sort(key=lambda item: (item.kind.value, _earliest_start_ms(item), str(item.id)))

    return [
        {
            "representation_id": str(item.id),
            "resource_version_id": str(item.resource_version_id),
            "kind": item.kind.value,
            "status": item.status.value,
            "locators": [locator.model_dump(mode="json") for locator in item.locators],
            "coverage_fraction": item.coverage.coverage_fraction,
        }
        for item in representations[:limit]
    ]


@mcp.tool()
def get_file_summary(file_id: str) -> FileRecord:
    """Cached summary + metadata for one file (no re-read of the file)."""
    svc = _services()
    rec = svc["records"].get(file_id)
    if rec is None:
        # fallback: try graph
        node = svc["graph"].get_file(file_id)
        if node is None:
            raise ValueError(f"unknown file_id: {file_id}")
        return node
    return rec


@mcp.tool()
def related(file_id: str, kinds: list[str] | None = None) -> list[FileHit]:
    """Graph neighbors: shared entities/topics, references, duplicates."""
    svc = _services()
    nbs = svc["graph"].neighbors(file_id, hops=1)
    if kinds:
        nbs = [n for n in nbs if n.get("via") in kinds]
    hits: list[FileHit] = []
    seen: set[str] = set()
    for nb in nbs:
        peer = nb.get("file_id")
        if peer is None or peer in seen or peer == file_id:
            continue
        seen.add(peer)
        node = svc["graph"].get_file(peer)
        if node is None:
            rec = svc["records"].get(peer)
            if rec is None:
                continue
            path = rec.path
            summary = rec.summary or ""
        else:
            path = node.path
            summary = node.summary or ""
        hits.append(
            FileHit(
                file_id=peer,
                path=path,
                summary=summary,
                score=nb.get("score", 0.0),
                why=nb.get("via", "neighbor"),
            )
        )
    return hits


@mcp.tool()
def index_file_tool(path: str) -> FileRecord:
    """Index a single file via the ingest pipeline. Useful for ad-hoc ingestion
    from the client without running the CLI."""
    svc = _services()
    return svc["pipeline"].index_file(Path(path))


@mcp.tool()
def answer(query: str, mode: str | None = None) -> dict:
    """Synthesis over the curated context bundle. Supports multiple backends.
    OFF BY DEFAULT (set katsi.mcp.enable_answer_tool=true to enable)."""
    svc = _services()
    s = svc["settings"]
    if not s.mcp.enable_answer_tool:
        raise PermissionError(
            "answer tool disabled; set katsi.mcp.enable_answer_tool=true to enable"
        )
    bundle = build_context(
        query,
        max_tokens=s.retrieve.default_context_max_tokens,
        settings=s,
        vectors=svc["vectors"],
        graph=svc["graph"],
        embed=svc["embed"],
        records=svc["records"],
    )
    synth = build_synthesizer(s, mode=mode, llm_client=svc["llm"])
    result = synth.answer(query, bundle)
    if result.text is None:
        return {
            "text": None,
            "mode": "return_only",
            "escalated": False,
            "hint": "use get_context for the bundle",
        }
    return {"text": result.text, "mode": result.mode, "escalated": result.escalated}


# --- Workspace coordination MCP tools ---


@mcp.tool()
def open_workspace(root_path: str) -> dict:
    """Open or register a workspace by its root path.

    Returns the workspace ID, display name, and current state version.
    Requires authenticated agent identity if configured.
    """
    from pathlib import Path

    svc = _services()
    root = Path(root_path).resolve()

    # Try to find existing workspace by root
    database = svc["workspace_database"]
    with database.connection() as conn:
        existing = conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?", (str(root),)
        ).fetchone()

    if existing:
        from katsi_core.workspace.contracts import Workspace

        workspace = Workspace(
            id=existing["id"],
            root_path=existing["root_path"],
            display_name=existing["display_name"],
            status=existing["status"],
            state_version=existing["state_version"],
            created_at=existing["created_at"],
            updated_at=existing["updated_at"],
        )
        return {
            "workspace_id": str(workspace.id),
            "display_name": workspace.display_name,
            "status": workspace.status,
            "state_version": workspace.state_version,
            "root_path": workspace.root_path,
            "created_at": workspace.created_at.isoformat(),
            "updated_at": workspace.updated_at.isoformat(),
        }

    # Register new workspace
    workspace = svc["workspace_repository"].register_workspace(root, f"workspace-{root.name}")
    return {
        "workspace_id": str(workspace.id),
        "display_name": workspace.display_name,
        "status": workspace.status,
        "state_version": workspace.state_version,
        "root_path": workspace.root_path,
        "created_at": workspace.created_at.isoformat(),
        "updated_at": workspace.updated_at.isoformat(),
    }


@mcp.tool()
def inspect_workspace(workspace_id: str) -> dict:
    """Inspect the current state of a workspace.

    Returns workspace metadata, status, and recent activity.
    """
    from uuid import UUID

    svc = _services()
    workspace = svc["workspace_repository"].get_workspace(UUID(workspace_id))

    if workspace is None:
        raise ValueError(f"workspace not found: {workspace_id}")

    # Get recent events
    recent_events = list(svc["workspace_repository"].recent_events(UUID(workspace_id), limit=10))

    return {
        "workspace_id": str(workspace.id),
        "display_name": workspace.display_name,
        "status": workspace.status,
        "state_version": workspace.state_version,
        "root_path": workspace.root_path,
        "created_at": workspace.created_at.isoformat(),
        "updated_at": workspace.updated_at.isoformat(),
        "recent_events": [
            {
                "sequence": event.sequence,
                "kind": event.kind,
                "occurred_at": event.occurred_at.isoformat(),
                "detail": event.detail,
            }
            for event in recent_events
        ],
    }


@mcp.tool()
def get_workspace_brief(workspace_id: str, byte_budget: int = 100000) -> dict:
    """Get a task-scoped Workspace Brief for the authenticated agent identity.

    The brief includes claims, decisions, blockers, open questions, active work,
    leases, and recent events, all bounded by the byte budget to provide focused context.

    Args:
        workspace_id: The workspace UUID
        byte_budget: Maximum bytes for the brief content (default: 100KB)

    Returns a budgeted brief with the most relevant workspace state.
    """
    from uuid import UUID

    svc = _services()
    brief = svc["brief_service"].assemble(UUID(workspace_id), byte_budget=byte_budget)

    return {
        "workspace_id": str(brief.workspace_id),
        "state_version": brief.state_version,
        "last_event_sequence": brief.last_event_sequence,
        "intent": {"goal": brief.intent[0], "version": brief.intent[1]} if brief.intent else None,
        "claims": [
            {
                "id": str(c.id),
                "text": c.text,
                "author_id": str(c.author_id),
                "status": c.status,
                "confidence": c.confidence,
                "scope_paths": c.scope_paths,
                "created_at": c.created_at.isoformat(),
                "invalidated": c.invalidated,
            }
            for c in brief.claims
        ],
        "decisions": [
            {
                "id": str(d.id),
                "kind": d.kind,
                "text": d.text,
                "status": d.status,
                "author_id": str(d.author_id),
                "created_at": d.created_at.isoformat(),
            }
            for d in brief.decisions
        ],
        "blockers": [
            {
                "id": str(b.id),
                "text": b.text,
                "author_id": str(b.author_id),
                "created_at": b.created_at.isoformat(),
            }
            for b in brief.blockers
        ],
        "open_questions": [
            {
                "id": str(q.id),
                "text": q.text,
                "author_id": str(q.author_id),
                "created_at": q.created_at.isoformat(),
            }
            for q in brief.open_questions
        ],
        "open_work": [
            {
                "id": str(w.id),
                "description": w.description,
                "status": w.status,
                "author_id": str(w.author_id),
                "created_at": w.created_at.isoformat(),
            }
            for w in brief.open_work
        ],
        "leases": [
            {
                "id": str(lease.id),
                "holder_id": str(lease.holder_id),
                "task_description": lease.task_description,
                "resource_scope": lease.resource_scope,
                "expires_at": lease.expires_at.isoformat(),
            }
            for lease in brief.leases
        ],
        "recent_events": [
            {
                "event_sequence": e.event_sequence,
                "kind": e.kind,
                "occurred_at": e.occurred_at.isoformat(),
                "path": e.path,
                "detail": e.detail,
            }
            for e in brief.recent_events
        ],
        "budget_bytes": brief.budget_bytes,
        "bytes_used": brief.bytes_used,
        "omitted": [
            {"section": o.section, "count": o.count, "reason": o.reason} for o in brief.omitted
        ],
        "provisional": [p.value for p in brief.provisional],
        "projection_lag": brief.projection_lag,
    }


@mcp.tool()
def publish_claim(
    workspace_id: str,
    text: str,
    scope_paths: list[str] | None = None,
    confidence: float = 0.8,
    media_evidence: list[dict[str, str]] | None = None,
) -> dict:
    """Publish a new Claim with capability checking.

    Creates a proposed Claim attributed to the authenticated agent identity.
    The claim text and scope paths never change after publication.

    Args:
        workspace_id: The workspace UUID
        text: The claim text (max 20000 chars)
        scope_paths: Optional workspace-relative paths this claim applies to
        confidence: Confidence score 0-1 (default: 0.8)

    Returns the published Claim with its ID and status.
    """
    from datetime import UTC, datetime
    from uuid import UUID, uuid4

    svc = _services()

    # Check authorization
    identity = svc.get("authenticated_identity")
    if not identity:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")

    # Authorize the operation
    try:
        svc["identity_service"].authorize(
            identity.id,
            UUID(workspace_id),
            CapabilityOperationClass.CLAIM,
            None,
            RiskClass.LOW,
        )
    except Exception as e:
        # Redact specific authorization details in error
        raise PermissionError("authorization denied for claim operation") from e

    claim = Claim(
        id=uuid4(),
        workspace_id=UUID(workspace_id),
        author_id=identity.id,
        text=text,
        scope_paths=tuple(scope_paths or []),
        confidence=confidence,
        status=ClaimStatus.PROPOSED,
        created_at=datetime.now(UTC),
    )

    from katsi_core.workspace.contracts import ClaimEvidence, ClaimEvidenceKind

    evidence = []
    for item in media_evidence or []:
        required = {"representation_id", "resource_version_id", "locator"}
        if not required <= item.keys():
            raise ValueError(
                "media evidence requires representation_id, resource_version_id, and locator"
            )
        # Locator is serialized at the boundary so the append-only, portable
        # Claim evidence schema stays compatible with existing workspaces.
        evidence.append(
            ClaimEvidence(
                id=uuid4(),
                claim_id=claim.id,
                kind=ClaimEvidenceKind.AGENT,
                reference={key: str(value) for key, value in item.items()},
                created_at=datetime.now(UTC),
            )
        )
    published = svc["claim_service"].publish(claim, tuple(evidence))
    return {
        "claim_id": str(published.id),
        "workspace_id": str(published.workspace_id),
        "author_id": str(published.author_id),
        "text": published.text,
        "scope_paths": published.scope_paths,
        "confidence": published.confidence,
        "status": published.status,
        "created_at": published.created_at.isoformat(),
    }


@mcp.tool()
def list_claims(workspace_id: str, status: str | None = None) -> list[dict]:
    """List Claims for a workspace, optionally filtered by status.

    Returns all claims with their current verification state.
    Filter by status: proposed, corroborated, verified, invalidated, contradicted, superseded.
    """
    from uuid import UUID

    svc = _services()
    claims = svc["claim_service"].list_for_workspace(UUID(workspace_id))

    if status:
        claims = [c for c in claims if c.status == status]

    return [
        {
            "claim_id": str(c.id),
            "workspace_id": str(c.workspace_id),
            "author_id": str(c.author_id),
            "text": c.text,
            "scope_paths": c.scope_paths,
            "confidence": c.confidence,
            "status": c.status,
            "created_at": c.created_at.isoformat(),
        }
        for c in claims
    ]


@mcp.tool()
def inspect_decisions(workspace_id: str, status: str | None = None) -> list[dict]:
    """Inspect workspace decisions (decisions requiring owner verification).

    Returns verified and open decisions for the workspace.
    """
    from uuid import UUID

    from katsi_core.workspace.contracts import WorkspaceRecordKind

    svc = _services()
    records = svc["record_service"].list_records(UUID(workspace_id))

    decisions = [r for r in records if r.kind == WorkspaceRecordKind.DECISION]
    if status:
        decisions = [d for d in decisions if d.status == status]

    return [
        {
            "id": str(d.id),
            "workspace_id": str(d.workspace_id),
            "kind": d.kind,
            "text": d.text,
            "status": d.status,
            "author_id": str(d.author_id),
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat(),
        }
        for d in decisions
    ]


@mcp.tool()
def inspect_blockers(workspace_id: str) -> list[dict]:
    """Inspect open blockers preventing work completion.

    Returns all active (open) blockers for the workspace.
    """
    from uuid import UUID

    from katsi_core.workspace.contracts import WorkspaceRecordKind, WorkspaceRecordStatus

    svc = _services()
    records = svc["record_service"].list_records(UUID(workspace_id))

    blockers = [
        r
        for r in records
        if r.kind == WorkspaceRecordKind.BLOCKER and r.status == WorkspaceRecordStatus.OPEN
    ]

    return [
        {
            "id": str(b.id),
            "workspace_id": str(b.workspace_id),
            "text": b.text,
            "author_id": str(b.author_id),
            "created_at": b.created_at.isoformat(),
            "updated_at": b.updated_at.isoformat(),
        }
        for b in blockers
    ]


@mcp.tool()
def inspect_open_work(workspace_id: str) -> list[dict]:
    """Inspect open work items tracked for the workspace.

    Returns active open work items and their status.
    """
    from uuid import UUID

    from katsi_core.workspace.contracts import OpenWorkStatus

    svc = _services()
    work_items = svc["record_service"].list_open_work(UUID(workspace_id))

    # Filter for active work (open or blocked)
    active_work = [
        w for w in work_items if w.status in (OpenWorkStatus.OPEN, OpenWorkStatus.BLOCKED)
    ]

    return [
        {
            "id": str(w.id),
            "workspace_id": str(w.workspace_id),
            "description": w.description,
            "status": w.status,
            "author_id": str(w.author_id),
            "created_at": w.created_at.isoformat(),
            "updated_at": w.updated_at.isoformat(),
        }
        for w in active_work
    ]


@mcp.tool()
def acquire_work_lease(
    workspace_id: str,
    task_description: str,
    resource_scope: list[str] | None = None,
) -> dict:
    """Acquire an advisory Work Lease for active agent work.

    Creates a time-bounded visible lease showing the agent's current work focus.
    Requires authenticated agent identity.

    Args:
        workspace_id: The workspace UUID
        task_description: Description of the work being performed
        resource_scope: Optional paths this work covers

    Returns the acquired lease with expiration time.
    """
    from uuid import UUID

    svc = _services()

    # Check authentication
    identity = svc.get("authenticated_identity")
    if not identity:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")

    # Authorize the operation
    try:
        svc["identity_service"].authorize(
            identity.id,
            UUID(workspace_id),
            CapabilityOperationClass.LEASE,
            None,
            RiskClass.LOW,
        )
    except Exception as e:
        raise PermissionError("authorization denied for lease operation") from e

    lease = svc["lease_service"].acquire(
        workspace_id=UUID(workspace_id),
        holder_id=identity.id,
        task_description=task_description,
        resource_scope=tuple(resource_scope or []),
    )

    return {
        "lease_id": str(lease.id),
        "workspace_id": str(lease.workspace_id),
        "holder_id": str(lease.holder_id),
        "kind": lease.kind,
        "status": lease.status,
        "task_description": lease.task_description,
        "resource_scope": lease.resource_scope,
        "acquired_at": lease.acquired_at.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
    }


@mcp.tool()
def renew_work_lease(lease_id: str, expected_expires_at: str) -> dict:
    """Renew an active Work Lease before it expires.

    Extends the lease expiration time. Requires the same holder identity
    that acquired the lease and the current expected expiration.

    Args:
        lease_id: The lease UUID to renew
        expected_expires_at: Current expected expiration time (ISO format)

    Returns the renewed lease with new expiration.
    """
    from datetime import datetime
    from uuid import UUID

    svc = _services()

    # Check authentication
    identity = svc.get("authenticated_identity")
    if not identity:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")

    renewed = svc["lease_service"].renew(
        lease_id=UUID(lease_id),
        holder_id=identity.id,
        expected_expires_at=datetime.fromisoformat(expected_expires_at),
    )

    return {
        "lease_id": str(renewed.id),
        "status": renewed.status,
        "expires_at": renewed.expires_at.isoformat(),
        "released_at": renewed.released_at.isoformat() if renewed.released_at else None,
    }


@mcp.tool()
def release_work_lease(lease_id: str) -> dict:
    """Release an active Work Lease.

    Marks the lease as released. Only the lease holder may release it.
    Requires authenticated agent identity.

    Args:
        lease_id: The lease UUID to release

    Returns the released lease with release timestamp.
    """
    from uuid import UUID

    svc = _services()

    # Check authentication
    identity = svc.get("authenticated_identity")
    if not identity:
        raise PermissionError("Authentication required: set KATSI_AGENT_CREDENTIAL")

    released = svc["lease_service"].release(
        lease_id=UUID(lease_id),
        holder_id=identity.id,
    )

    return {
        "lease_id": str(released.id),
        "status": released.status,
        "released_at": released.released_at.isoformat(),
    }


@mcp.tool()
def inspect_active_leases(workspace_id: str) -> list[dict]:
    """Inspect all active Work Leases for a workspace.

    Returns currently active (not expired/released) leases showing
    concurrent work activity.
    """
    from uuid import UUID

    svc = _services()
    leases = svc["lease_service"].active_for_workspace(UUID(workspace_id))

    return [
        {
            "lease_id": str(lease.id),
            "holder_id": str(lease.holder_id),
            "task_description": lease.task_description,
            "resource_scope": lease.resource_scope,
            "kind": lease.kind,
            "status": lease.status,
            "acquired_at": lease.acquired_at.isoformat(),
            "expires_at": lease.expires_at.isoformat(),
        }
        for lease in leases
    ]


def main() -> None:
    """Entry point: `katsi-mcp` console script."""
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
