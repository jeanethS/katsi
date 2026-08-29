import json
from io import BytesIO

from katsi_app.app import create_app, get_graph, get_status
from starlette.testclient import TestClient

from katsi_core.config import Settings
from katsi_core.ingest.records import FileRecordStore
from katsi_core.models import FileRecord
from katsi_core.store.graph import GraphStore


def test_status_endpoint_returns_frontend_contract() -> None:
    expected = {
        "ollama": {"reachable": True, "models": ["embed", "chat"]},
        "counts": {"files": 2, "chunks": 3, "entities": 4, "topics": 5},
        "db_bytes": {"vectors": 6, "graph": 7},
        "synth": {"backend": "local", "cloud_configured": False},
    }

    response = TestClient(create_app(lambda: expected)).get("/api/status")

    assert response.status_code == 200
    assert response.json() == expected


def test_graph_endpoint_returns_frontend_contract() -> None:
    expected = {
        "nodes": [{"id": "file:one", "label": "one.md", "type": "file"}],
        "edges": [],
    }

    response = TestClient(create_app(graph_provider=lambda: expected)).get("/api/graph")

    assert response.status_code == 200
    assert response.json() == expected


def test_index_endpoint_passes_selected_folder_to_provider() -> None:
    response = TestClient(
        create_app(index_provider=lambda path: {"indexed": 2, "skipped": 1, "error": 0, "total": 3})
    ).post("/api/index", json={"path": "/tmp/notes"})

    assert response.status_code == 200
    assert response.json() == {"indexed": 2, "skipped": 1, "error": 0, "total": 3}


def test_index_endpoint_requires_a_folder_path() -> None:
    response = TestClient(create_app(index_provider=lambda _path: {})).post("/api/index", json={})

    assert response.status_code == 400


def test_get_graph_reports_empty_without_inventing_nodes(tmp_path) -> None:
    settings = Settings(store={"data_dir": tmp_path})

    payload = get_graph(settings)

    assert payload == {"nodes": [], "edges": [], "status": "empty"}


def test_get_graph_reports_unavailable_when_the_store_fails(tmp_path, monkeypatch) -> None:
    settings = Settings(store={"data_dir": tmp_path})

    def fail(*_args, **_kwargs):
        raise RuntimeError("kuzu is locked")

    monkeypatch.setattr("katsi_app.app.GraphStore", fail)

    payload = get_graph(settings)

    assert payload == {"nodes": [], "edges": [], "status": "unavailable"}


def test_get_graph_returns_indexed_nodes(tmp_path) -> None:
    settings = Settings(store={"data_dir": tmp_path})
    graph = GraphStore(tmp_path / settings.store.kuzu_db)
    graph.upsert_file(
        FileRecord(
            id="one",
            path="one.md",
            name="one.md",
            ext=".md",
            mime="text/markdown",
            size_bytes=1,
            mtime=1,
            content_hash="hash",
            summary="A note",
        )
    )
    graph.add_about("one", ["AI"])
    graph.close()

    payload = get_graph(settings)

    assert payload["status"] == "ready"
    assert {node["id"] for node in payload["nodes"]} == {"one", "topic:AI"}
    assert payload["edges"] == [
        {"source": "one", "target": "topic:AI", "type": "about", "weight": 1.0}
    ]


def test_get_status_reads_local_services(tmp_path, monkeypatch) -> None:
    settings = Settings(store={"data_dir": tmp_path})
    FileRecordStore(tmp_path / "records").put(
        FileRecord(
            id="one",
            path="one.md",
            name="one.md",
            ext=".md",
            mime="text/markdown",
            size_bytes=1,
            mtime=1,
            content_hash="hash",
        )
    )
    graph = GraphStore(tmp_path / settings.store.kuzu_db)
    graph.upsert_entity("Acme", "organization")
    graph.upsert_topic("AI")
    graph.close()
    payload = json.dumps({"models": [{"name": "local-chat"}]}).encode()
    request: dict[str, object] = {}

    def fake_urlopen(url: str, *, timeout: float) -> BytesIO:
        request.update(url=url, timeout=timeout)
        return BytesIO(payload)

    monkeypatch.setattr("katsi_app.app.urlopen", fake_urlopen)

    status = get_status(settings)

    assert status["ollama"] == {"reachable": True, "models": ["local-chat"]}
    assert status["counts"] == {"files": 1, "chunks": 0, "entities": 1, "topics": 1}
    assert status["db_bytes"]["graph"] > 0
    assert status["synth"] == {"backend": "return_only", "cloud_configured": False}
    assert request["timeout"] == settings.ollama.timeout
