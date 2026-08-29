from __future__ import annotations

import json
import os
from collections.abc import Callable
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
from katsi_core.ingest.walk import walk_files
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

StatusProvider = Callable[[], dict[str, object]]
GraphProvider = Callable[[], dict[str, object]]
IndexProvider = Callable[[str], dict[str, object]]


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())
    return 0


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


def get_graph(settings: Settings) -> dict[str, object]:
    """Return the graph payload for the frontend galaxy view.

    `status` tells the frontend which empty is which: a real but unpopulated
    library ("empty") versus a graph it could not read ("unavailable"). Never
    substitutes sample data — a fabricated node reads as the user's own file.
    """
    graph: GraphStore | None = None
    try:
        graph = GraphStore(settings.store.data_dir / settings.store.kuzu_db)
        payload = graph.export_graph()
    except Exception:
        return {"nodes": [], "edges": [], "status": "unavailable"}
    finally:
        if graph is not None:
            graph.close()
    return {**payload, "status": "ready" if payload["nodes"] else "empty"}


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
        files = walk_files(root, settings.ingest.include_globs, settings.ingest.exclude_globs)
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
