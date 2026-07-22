from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from katsi_core.config import Settings, get_settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore

StatusProvider = Callable[[], dict[str, object]]


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


def create_app(status_provider: StatusProvider | None = None) -> Starlette:
    provider = status_provider or (lambda: get_status(get_settings()))

    def status(_request: Request) -> JSONResponse:
        return JSONResponse(provider())

    return Starlette(routes=[Route("/api/status", status)])


app = create_app()


def main() -> None:
    uvicorn.run("katsi_app.app:app", host="127.0.0.1", port=8000)
