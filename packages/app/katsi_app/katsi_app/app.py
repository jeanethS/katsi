from __future__ import annotations

import json
import os
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from katsi_core.clients.embed import EmbedClient
from katsi_core.clients.llm import LLMClient
from katsi_core.config import Settings, get_settings
from katsi_core.ingest.pipeline import IngestPipeline
from katsi_core.ingest.records import FileRecordStore
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

StatusProvider = Callable[[], dict[str, object]]
GraphProvider = Callable[[], dict[str, object]]
IndexProvider = Callable[[str], dict[str, object]]


def _unwrap(value: object) -> object:
    """Return the bare Python value from a kuzu Value wrapper, or the value itself."""
    return value.value if hasattr(value, "value") else value


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    return 0


def _demo_graph() -> dict[str, object]:
    """Return a deterministic demo graph used when the real graph is empty or unreachable."""
    nodes = [
        {
            "id": "file:notes",
            "label": "notes",
            "type": "file",
            "path": "/home/j/notes",
            "summary": "Personal notes vault",
        },
        {
            "id": "file:research",
            "label": "research",
            "type": "file",
            "path": "/home/j/research",
            "summary": "Research materials",
        },
        {
            "id": "file:projects",
            "label": "projects",
            "type": "file",
            "path": "/home/j/projects",
            "summary": "Active projects",
        },
        {
            "id": "file:china-trip",
            "label": "china-trip.md",
            "type": "file",
            "path": "/home/j/notes/china-trip.md",
            "summary": "Notes on payment adoption and local infrastructure",
        },
        {
            "id": "file:venus-pitch",
            "label": "venus-pitch.md",
            "type": "file",
            "path": "/home/j/notes/venus-pitch.md",
            "summary": "Pitch about shared infrastructure",
        },
        {"id": "entity:Alipay", "label": "Alipay", "type": "entity", "kind": "organization"},
        {"id": "entity:fintech", "label": "fintech", "type": "entity", "kind": "topic"},
        {
            "id": "entity:infrastructure",
            "label": "infrastructure",
            "type": "entity",
            "kind": "concept",
        },
        {"id": "topic:coordination", "label": "coordination", "type": "topic"},
        {"id": "topic:payments", "label": "payments", "type": "topic"},
    ]
    edges = [
        {"source": "file:china-trip", "target": "entity:Alipay", "type": "mentions", "weight": 1.0},
        {
            "source": "file:china-trip",
            "target": "entity:fintech",
            "type": "mentions",
            "weight": 0.8,
        },
        {
            "source": "file:venus-pitch",
            "target": "entity:infrastructure",
            "type": "mentions",
            "weight": 0.9,
        },
        {
            "source": "file:venus-pitch",
            "target": "topic:coordination",
            "type": "about",
            "weight": 1.0,
        },
        {"source": "file:china-trip", "target": "topic:payments", "type": "about", "weight": 0.9},
        {"source": "file:notes", "target": "file:china-trip", "type": "references", "weight": 1.0},
        {"source": "file:notes", "target": "file:venus-pitch", "type": "references", "weight": 1.0},
        {"source": "file:research", "target": "file:projects", "type": "duplicate", "weight": 0.72},
    ]
    return {"nodes": nodes, "edges": edges}


def _ollama_status(settings: Settings) -> dict[str, object]:
    try:
        with urlopen(
            f"{settings.ollama.host.rstrip('/')}/api/tags",
            timeout=settings.ollama.timeout,
        ) as response:
            payload = json.load(response)
        return {
            "reachable": True,
            "models": [model["name"] for model in payload.get("models", [])],
        }
    except (KeyError, TypeError, ValueError, OSError, URLError):
        return {"reachable": False, "models": []}


def _query_graph(graph: GraphStore) -> dict[str, object]:
    """Read a bounded, frontend-friendly graph from Kùzu."""
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []

    file_ids: set[str] = set()
    entity_ids: set[str] = set()
    topic_ids: set[str] = set()

    files = graph._conn.execute("MATCH (f:File) RETURN f.id, f.name, f.path, f.summary LIMIT 100")
    while files.has_next():
        row = files.get_next()
        file_id = str(_unwrap(row[0]))
        file_ids.add(file_id)
        nodes.append(
            {
                "id": file_id,
                "label": str(_unwrap(row[1])) or file_id,
                "type": "file",
                "path": str(_unwrap(row[2])),
                "summary": str(_unwrap(row[3])),
            }
        )

    entities = graph._conn.execute("MATCH (e:Entity) RETURN e.name, e.kind LIMIT 50")
    while entities.has_next():
        row = entities.get_next()
        name = str(_unwrap(row[0]))
        entity_ids.add(name)
        nodes.append(
            {
                "id": f"entity:{name}",
                "label": name,
                "type": "entity",
                "kind": str(_unwrap(row[1])),
            }
        )

    topics = graph._conn.execute("MATCH (t:Topic) RETURN t.name LIMIT 50")
    while topics.has_next():
        row = topics.get_next()
        name = str(_unwrap(row[0]))
        topic_ids.add(name)
        nodes.append(
            {
                "id": f"topic:{name}",
                "label": name,
                "type": "topic",
            }
        )

    mentions = graph._conn.execute(
        "MATCH (f:File)-[m:MENTIONS]->(e:Entity) RETURN f.id, e.name, m.weight"
    )
    while mentions.has_next():
        row = mentions.get_next()
        file_id = str(_unwrap(row[0]))
        entity_name = str(_unwrap(row[1]))
        if file_id in file_ids and entity_name in entity_ids:
            edges.append(
                {
                    "source": file_id,
                    "target": f"entity:{entity_name}",
                    "type": "mentions",
                    "weight": float(_unwrap(row[2])),
                }
            )

    about = graph._conn.execute("MATCH (f:File)-[a:ABOUT]->(t:Topic) RETURN f.id, t.name, a.weight")
    while about.has_next():
        row = about.get_next()
        file_id = str(_unwrap(row[0]))
        topic_name = str(_unwrap(row[1]))
        if file_id in file_ids and topic_name in topic_ids:
            edges.append(
                {
                    "source": file_id,
                    "target": f"topic:{topic_name}",
                    "type": "about",
                    "weight": float(_unwrap(row[2])),
                }
            )

    references = graph._conn.execute("MATCH (f:File)-[:REFERENCES]->(o:File) RETURN f.id, o.id")
    while references.has_next():
        row = references.get_next()
        source_id = str(_unwrap(row[0]))
        target_id = str(_unwrap(row[1]))
        if source_id in file_ids and target_id in file_ids:
            edges.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "type": "references",
                    "weight": 1.0,
                }
            )

    duplicates = graph._conn.execute(
        "MATCH (f:File)-[d:DUPLICATE_OF]->(o:File) RETURN f.id, o.id, d.similarity"
    )
    while duplicates.has_next():
        row = duplicates.get_next()
        source_id = str(_unwrap(row[0]))
        target_id = str(_unwrap(row[1]))
        if source_id in file_ids and target_id in file_ids:
            edges.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "type": "duplicate",
                    "weight": float(_unwrap(row[2])),
                }
            )

    return {"nodes": nodes, "edges": edges}


def get_graph(settings: Settings) -> dict[str, object]:
    """Return a graph payload for the frontend galaxy view, with a graceful demo fallback."""
    data_dir = settings.store.data_dir
    graph = GraphStore(data_dir / settings.store.kuzu_db)
    try:
        payload = _query_graph(graph)
        return payload if payload["nodes"] else _demo_graph()
    except Exception:
        return _demo_graph()
    finally:
        graph.close()


def index_path(settings: Settings, path: str) -> dict[str, object]:
    """Index a user-selected local folder and return a compact completion summary."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Select an existing folder.")

    graph = GraphStore(settings.store.data_dir / settings.store.kuzu_db)
    vectors = VectorStore(settings.store.data_dir / "vectors", settings.store.lancedb_table)
    records = FileRecordStore(settings.store.data_dir / "records")
    pipeline = IngestPipeline(
        settings,
        graph=graph,
        vectors=vectors,
        embed=EmbedClient(settings),
        llm=LLMClient(settings),
        records=records,
    )
    try:
        files = [
            candidate
            for candidate in root.rglob("*")
            if candidate.is_file()
            and any(
                fnmatch(str(candidate), pattern) or fnmatch(candidate.name, pattern)
                for pattern in settings.ingest.include_globs
            )
            and not any(
                fnmatch(str(candidate), pattern) for pattern in settings.ingest.exclude_globs
            )
        ]
        counts = {"indexed": 0, "skipped": 0, "error": 0, "total": len(files)}
        for candidate in files:
            try:
                record = pipeline.index_file(candidate)
            except Exception:
                counts["error"] += 1
                continue
            if record.status.value == "indexed":
                counts["indexed"] += 1
            elif record.status.value == "error":
                counts["error"] += 1
            else:
                counts["skipped"] += 1
        return counts
    finally:
        graph.close()


def get_status(settings: Settings) -> dict[str, object]:
    data_dir = settings.store.data_dir
    records = FileRecordStore(data_dir / "records")
    vectors = VectorStore(data_dir / "vectors", settings.store.lancedb_table)
    graph = GraphStore(data_dir / settings.store.kuzu_db)
    try:
        try:
            chunks = vectors.count()
        except (AttributeError, OSError, ValueError):
            chunks = 0
        return {
            "ollama": _ollama_status(settings),
            "counts": {
                "files": len(records.list_all()),
                "chunks": chunks,
                **graph.count_nodes(),
            },
            "db_bytes": {
                "vectors": _path_size(data_dir / "vectors"),
                "graph": _path_size(data_dir / settings.store.kuzu_db),
            },
            "synth": {
                "backend": settings.synth.backend,
                "cloud_configured": bool(os.getenv(settings.synth.cloud.api_key_env)),
            },
        }
    finally:
        graph.close()


def create_app(
    status_provider: StatusProvider | None = None,
    graph_provider: GraphProvider | None = None,
    index_provider: IndexProvider | None = None,
) -> Starlette:
    status = status_provider or (lambda: get_status(get_settings()))
    graph = graph_provider or (lambda: get_graph(get_settings()))
    index = index_provider or (lambda path: index_path(get_settings(), path))

    def status_route(_request: Request) -> JSONResponse:
        return JSONResponse(status())

    def graph_route(_request: Request) -> JSONResponse:
        return JSONResponse(graph())

    async def index_route(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            path = payload.get("path") if isinstance(payload, dict) else None
            if not isinstance(path, str) or not path.strip():
                return JSONResponse({"detail": "A folder path is required."}, status_code=400)
            return JSONResponse(await run_in_threadpool(index, path))
        except ValueError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)

    return Starlette(
        routes=[
            Route("/api/status", status_route),
            Route("/api/graph", graph_route),
            Route("/api/index", index_route, methods=["POST"]),
        ]
    )


app = create_app()


def main() -> None:
    uvicorn.run("katsi_app.app:app", host="127.0.0.1", port=8000)
