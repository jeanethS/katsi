import json
from io import BytesIO

from katsi_app.app import create_app, get_status
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
